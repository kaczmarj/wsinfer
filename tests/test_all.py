import json
from pathlib import Path
import sys

from click.testing import CliRunner
import numpy as np
import pandas as pd
import pytest
import tifffile


@pytest.fixture
def tiff_image(tmp_path: Path) -> Path:
    x = np.empty((4096, 4096, 3), dtype="uint8")
    x[...] = [160, 32, 240]  # rgb for purple
    path = Path(tmp_path / "images" / "purple.tif")
    path.parent.mkdir(exist_ok=True)

    if sys.version_info >= (3, 8):
        tifffile.imwrite(
            path,
            data=x,
            compression="zlib",
            tile=(256, 256),
            # 0.25 micrometers per pixel.
            resolution=(40000, 40000),
            resolutionunit=tifffile.RESUNIT.CENTIMETER,
        )
    else:
        # Earlier versions of tifffile do not have resolutionunit kwarg.
        tifffile.imwrite(
            path,
            data=x,
            compression="zlib",
            tile=(256, 256),
            # 0.25 micrometers per pixel.
            resolution=(40000, 40000, "CENTIMETER"),
        )

    return path


def test_cli_list():
    from wsinfer.cli.cli import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["list"])
    assert "resnet34" in result.output
    assert "TCGA-BRCA-v1" in result.output
    assert result.exit_code == 0


def test_cli_run_args(tmp_path: Path):
    """Test that (model and weights) or config is required."""
    from wsinfer.cli.cli import cli

    wsi_dir = tmp_path / "slides"
    wsi_dir.mkdir()

    runner = CliRunner()
    args = [
        "run",
        "--wsi-dir",
        wsi_dir,
        "--results-dir",
        tmp_path / "results",
    ]
    # No model, weights, or config.
    result = runner.invoke(cli, args)
    assert result.exit_code != 0
    assert "one of (model and weights) or config is required." in result.output

    # Only one of model and weights.
    result = runner.invoke(cli, [*args, "--model", "resnet34"])
    assert result.exit_code != 0
    assert "model and weights must both be set if one is set." in result.output
    result = runner.invoke(cli, [*args, "--weights", "TCGA-BRCA-v1"])
    assert result.exit_code != 0
    assert "model and weights must both be set if one is set." in result.output

    # config and model
    result = runner.invoke(cli, [*args, "--config", __file__, "--model", "resnet34"])
    assert result.exit_code != 0
    assert "model and weights are mutually exclusive with config." in result.output
    # config and weights
    result = runner.invoke(
        cli, [*args, "--config", __file__, "--weights", "TCGA-BRCA-v1"]
    )
    assert result.exit_code != 0
    assert "model and weights are mutually exclusive with config." in result.output


def test_cli_run_and_convert(tiff_image: Path, tmp_path: Path):
    """This is a form of a regression test."""
    from wsinfer.cli.cli import cli

    runner = CliRunner()
    results_dir = tmp_path / "inference"
    result = runner.invoke(
        cli,
        [
            "run",
            "--wsi-dir",
            tiff_image.parent,
            "--model",
            "resnet34",
            "--weights",
            "TCGA-BRCA-v1",
            "--results-dir",
            results_dir,
        ],
    )
    assert result.exit_code == 0
    assert (results_dir / "model-outputs").exists()
    df = pd.read_csv(results_dir / "model-outputs" / "purple.csv")
    assert df.columns.tolist() == [
        "slide",
        "minx",
        "miny",
        "width",
        "height",
        "prob_notumor",
        "prob_tumor",
    ]
    assert (df.loc[:, "slide"] == str(tiff_image)).all()
    assert (df.loc[:, "width"] == 350).all()
    assert (df.loc[:, "height"] == 350).all()
    assert (df.loc[:, "width"] == 350).all()
    assert np.allclose(df.loc[:, "prob_notumor"], 0.9525967836380005)
    assert np.allclose(df.loc[:, "prob_tumor"], 0.04740329459309578)

    # Test conversion scripts.
    geojson_dir = results_dir / "geojson"
    result = runner.invoke(cli, ["togeojson", str(results_dir), str(geojson_dir)])
    assert result.exit_code == 0
    with open(geojson_dir / "purple.json") as f:
        d = json.load(f)
    assert len(d["features"]) == 144

    for geojson_row in d["features"]:
        assert geojson_row["type"] == "Feature"
        assert geojson_row["id"] == "PathTileObject"
        assert geojson_row["geometry"]["type"] == "Polygon"

    # Check the probability values.
    assert all(
        np.allclose(dd["properties"]["measurements"][0]["value"], 0.9525967836380004)
        for dd in d["features"]
    )
    assert all(
        np.allclose(dd["properties"]["measurements"][1]["value"], 0.0474032945930957)
        for dd in d["features"]
    )

    # Check the names.
    assert all(
        dd["properties"]["measurements"][0]["name"] == "prob_notumor"
        for dd in d["features"]
    )
    assert all(
        dd["properties"]["measurements"][1]["name"] == "prob_tumor"
        for dd in d["features"]
    )

    # Check the coordinate values.
    for df_row, geojson_row in zip(df.itertuples(), d["features"]):
        maxx = df_row.minx + df_row.width
        maxy = df_row.miny + df_row.height
        df_coords = [
            [maxx, df_row.miny],
            [maxx, maxy],
            [df_row.minx, maxy],
            [df_row.minx, df_row.miny],
            [maxx, df_row.miny],
        ]
        assert [df_coords] == geojson_row["geometry"]["coordinates"]


@pytest.mark.xfail
def test_convert_to_sbu():
    # TODO: create a synthetic output and then convert it. Check that it is valid.
    assert False


def test_cli_run_from_config(tiff_image: Path, tmp_path: Path):
    """This is a form of a regression test."""
    import wsinfer
    from wsinfer.cli.cli import cli

    # Use config for resnet34 TCGA-BRCA-v1 weights.
    config = Path(wsinfer.__file__).parent / "modeldefs" / "resnet34_tcga-brca-v1.yaml"
    assert config.exists()

    runner = CliRunner()
    results_dir = tmp_path / "inference"
    result = runner.invoke(
        cli,
        [
            "run",
            "--wsi-dir",
            tiff_image.parent,
            "--config",
            config,
            "--results-dir",
            results_dir,
        ],
    )
    assert result.exit_code == 0
    assert (results_dir / "model-outputs").exists()
    df = pd.read_csv(results_dir / "model-outputs" / "purple.csv")
    assert df.columns.tolist() == [
        "slide",
        "minx",
        "miny",
        "width",
        "height",
        "prob_notumor",
        "prob_tumor",
    ]
    assert (df.loc[:, "slide"] == str(tiff_image)).all()
    assert (df.loc[:, "width"] == 350).all()
    assert (df.loc[:, "height"] == 350).all()
    assert (df.loc[:, "width"] == 350).all()
    assert np.allclose(df.loc[:, "prob_notumor"], 0.9525967836380005)
    assert np.allclose(df.loc[:, "prob_tumor"], 0.04740329459309578)


@pytest.mark.parametrize(
    "modeldef",
    [
        [],
        {},
        dict(name="foo", architecture="resnet34"),
        # Missing url
        dict(
            name="foo",
            architecture="resnet34",
            # url="foo",
            # url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # missing url_file_name when url is given
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            # url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # url and file used together
        dict(
            name="foo",
            architecture="resnet34",
            file=__file__,
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # nonexistent file
        dict(
            name="foo",
            architecture="resnet34",
            file="path/to/fake/file",
            # url="foo",
            # url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # num_classes missing
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            # num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # num classes not equal to len of class names
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=2,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform missing
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            # transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.resize_size missing
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.mean missing
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.std missing
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.resize_size non int
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=0.5, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.resize_size non int
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(
                resize_size=[100, 100], mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]
            ),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.mean not a list of three floats
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.mean not a list of three floats
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[1, 1, 1], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.mean not a list of three floats
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=0.5, std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.std not a list of three floats
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, std=[0.5], mean=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.std not a list of three floats
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, std=[1, 1, 1], mean=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # transform.std not a list of three floats
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, std=0.5, mean=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # invalid patch_size_pixels -- list
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=[350],
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # invalid patch_size_pixels -- float
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350.0,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # invalid patch_size_pixels -- negative
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=-100,
            spacing_um_px=0.25,
            class_names=["tumor"],
        ),
        # invalid spacing_um_px -- zero
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0,
            class_names=["tumor"],
        ),
        # invalid spacing_um_px -- list
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=[0.25],
            class_names=["tumor"],
        ),
        # invalid class_names -- str
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names="t",
        ),
        # invalid class_names -- len not equal to num_classes
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=["tumor", "nontumor"],
        ),
        # invalid class_names -- not list of str
        dict(
            name="foo",
            architecture="resnet34",
            url="foo",
            url_file_name="foo",
            num_classes=1,
            transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
            patch_size_pixels=350,
            spacing_um_px=0.25,
            class_names=[1],
        ),
    ],
)
def test_invalid_modeldefs(modeldef, tmp_path: Path):
    import yaml
    from wsinfer.modellib.models import Weights

    path = tmp_path / "foobar.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(modeldef, f)

    with pytest.raises(Exception):
        Weights.from_yaml(path)


def test_model_registration(tmp_path: Path):
    from wsinfer.modellib import models
    import yaml

    # Test that registering duplicate weights will error.
    d = dict(
        name="foo",
        architecture="resnet34",
        url="foo",
        url_file_name="foo",
        num_classes=1,
        transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
        patch_size_pixels=350,
        spacing_um_px=0.25,
        class_names=["foo"],
    )
    path = tmp_path / "foobar.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(d, f)
    path = tmp_path / "foobardup.yaml"
    with open(path, "w") as f:
        yaml.safe_dump(d, f)

    with pytest.raises(models.DuplicateModelWeights):
        models.register_model_weights(tmp_path)

    # Test that registering models will put them in the _known_model_weights object.
    path = tmp_path / "configs" / "foobar.yaml"
    path.parent.mkdir()
    d = dict(
        name="foo2",
        architecture="resnet34",
        url="foo",
        url_file_name="foo",
        num_classes=1,
        transform=dict(resize_size=299, mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
        patch_size_pixels=350,
        spacing_um_px=0.25,
        class_names=["foo"],
    )
    with open(path, "w") as f:
        yaml.safe_dump(d, f)
    models.register_model_weights(path.parent)
    assert (d["architecture"], d["name"]) in models._known_model_weights.keys()
    assert all(
        isinstance(m, models.Weights) for m in models._known_model_weights.values()
    )
