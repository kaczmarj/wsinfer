from __future__ import annotations

import json
import math
import os
import platform
import sys
import time
from pathlib import Path
from typing import List
import warnings

import geojson as geojsonlib
import h5py
import numpy as np
import pandas as pd
import pytest
import tifffile
import torch
import yaml
from click.testing import CliRunner


@pytest.fixture
def tiff_image(tmp_path: Path) -> Path:
    x = np.empty((4096, 4096, 3), dtype="uint8")
    x[...] = [160, 32, 240]  # rgb for purple
    path = Path(tmp_path / "images" / "purple.tif")
    path.parent.mkdir(exist_ok=True)

    tifffile.imwrite(
        path,
        data=x,
        compression="zlib",
        tile=(256, 256),
        # 0.25 micrometers per pixel.
        resolution=(40_000, 40_000),
        resolutionunit=tifffile.RESUNIT.CENTIMETER,
    )

    return path


# The reference data for this test was made using a patched version of wsinfer 0.3.6.
# The patches fixed an issue when calculating strides and added padding to images.
# Large-image (which was the backend in 0.3.6) did not pad images and would return
# tiles that were not fully the requested width and height.
@pytest.mark.parametrize(
    "model",
    [
        "breast-tumor-resnet34.tcga-brca",
        "breast-tumor-inception_v4.tcga-brca",
        "breast-tumor-vgg16mod.tcga-brca",
        "lung-tumor-resnet34.tcga-luad",
        "pancancer-lymphocytes-inceptionv4.tcga",
        "pancreas-tumor-preactresnet34.tcga-paad",
        "prostate-tumor-resnet34.tcga-prad",
    ],
)
@pytest.mark.parametrize("speedup", [False, True])
@pytest.mark.parametrize("backend", ["openslide", "tiffslide"])
def test_cli_run_with_registered_models(
    model: str,
    speedup: bool,
    backend: str,
    tiff_image: Path,
    tmp_path: Path,
):
    """A regression test of the command 'wsinfer run'."""
    from wsinfer.cli.cli import cli

    reference_csv = Path(__file__).parent / "reference" / model / "purple.csv"
    if not reference_csv.exists():
        raise FileNotFoundError(f"reference CSV not found: {reference_csv}")

    runner = CliRunner()
    results_dir = tmp_path / "inference"
    result = runner.invoke(
        cli,
        [
            "--backend",
            backend,
            "run",
            "--wsi-dir",
            str(tiff_image.parent),
            "--results-dir",
            str(results_dir),
            "--model",
            model,
            "--speedup" if speedup else "--no-speedup",
        ],
    )
    assert result.exit_code == 0
    assert (results_dir / "model-outputs").exists()
    df = pd.read_csv(results_dir / "model-outputs" / "purple.csv")
    df_ref = pd.read_csv(reference_csv)

    assert set(df.columns) == set(df_ref.columns)
    assert df.shape == df_ref.shape
    assert np.array_equal(df["minx"], df_ref["minx"])
    assert np.array_equal(df["miny"], df_ref["miny"])
    assert np.array_equal(df["width"], df_ref["width"])
    assert np.array_equal(df["height"], df_ref["height"])

    prob_cols = df_ref.filter(like="prob_").columns.tolist()
    for prob_col in prob_cols:
        assert np.allclose(
            df[prob_col], df_ref[prob_col], atol=1e-07
        ), f"Column {prob_col} not allclose at atol=1e-07"

    # Test that metadata path exists.
    metadata_paths = list(results_dir.glob("run_metadata_*.json"))
    assert len(metadata_paths) == 1
    metadata_path = metadata_paths[0]
    assert metadata_path.exists()
    with open(metadata_path) as f:
        meta = json.load(f)
    assert set(meta.keys()) == {"model", "runtime", "timestamp"}
    assert "config" in meta["model"]
    assert "huggingface_location" in meta["model"]
    assert model in meta["model"]["huggingface_location"]["repo_id"]
    assert meta["runtime"]["python_executable"] == sys.executable
    assert meta["runtime"]["python_version"] == platform.python_version()
    assert meta["timestamp"]
    del metadata_path, meta

    # Test conversion to geojson.
    geojson_dir = results_dir / "geojson"
    result = runner.invoke(cli, ["togeojson", str(results_dir), str(geojson_dir)])
    assert result.exit_code == 0
    with open(geojson_dir / "purple.json") as f:
        d: geojsonlib.GeoJSON = geojsonlib.load(f)
    assert d.is_valid, "geojson not valid!"
    assert len(d["features"]) == len(df_ref)

    for geojson_row in d["features"]:
        assert geojson_row["type"] == "Feature"
        assert geojson_row["id"] == "PathTileObject"
        assert geojson_row["geometry"]["type"] == "Polygon"

    res = []
    for i, _ in enumerate(prob_cols):
        res.append(
            np.array(
                [dd["properties"]["measurements"][i]["value"] for dd in d["features"]]
            )
        )
    geojson_probs = np.stack(res, axis=0)
    del res
    assert np.allclose(df[prob_cols].T, geojson_probs)

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


def test_cli_run_args(tmp_path: Path):
    """Test that (model and weights) or config is required."""
    from wsinfer.cli.cli import cli

    wsi_dir = tmp_path / "slides"
    wsi_dir.mkdir()

    runner = CliRunner()
    args = [
        "run",
        "--wsi-dir",
        str(wsi_dir),
        "--results-dir",
        str(tmp_path / "results"),
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


@pytest.mark.xfail
def test_convert_to_sbu():
    # TODO: create a synthetic output and then convert it. Check that it is valid.
    assert False


# def test_cli_run_from_config(tiff_image: Path, tmp_path: Path):
#     """This is a form of a regression test."""
#     import wsinfer
#     from wsinfer.cli.cli import cli

#     # Use config for resnet34 TCGA-BRCA-v1 weights.
#     config = Path(wsinfer.__file__).parent / "modeldefs" / "resnet34_tcga-brca-v1.yaml"
#     assert config.exists()

#     runner = CliRunner()
#     results_dir = tmp_path / "inference"
#     result = runner.invoke(
#         cli,
#         [
#             "run",
#             "--wsi-dir",
#             str(tiff_image.parent),
#             "--config",
#             str(config),
#             "--results-dir",
#             str(results_dir),
#         ],
#     )
#     assert result.exit_code == 0
#     assert (results_dir / "model-outputs").exists()
#     df = pd.read_csv(results_dir / "model-outputs" / "purple.csv")
#     assert df.columns.tolist() == [
#         "slide",
#         "minx",
#         "miny",
#         "width",
#         "height",
#         "prob_notumor",
#         "prob_tumor",
#     ]
#     assert (df.loc[:, "slide"] == str(tiff_image)).all()
#     assert (df.loc[:, "width"] == 350).all()
#     assert (df.loc[:, "height"] == 350).all()
#     assert (df.loc[:, "width"] == 350).all()
#     assert np.allclose(df.loc[:, "prob_notumor"], 0.9525967836380005)
#     assert np.allclose(df.loc[:, "prob_tumor"], 0.04740329459309578)


@pytest.mark.parametrize(
    ["patch_size", "patch_spacing"],
    [(256, 0.25), (256, 0.50), (350, 0.25), (100, 0.3)],
)
def test_patch_cli(
    patch_size: int, patch_spacing: float, tmp_path: Path, tiff_image: Path
):
    from wsinfer.cli.cli import cli

    orig_slide_width = 4096
    orig_slide_height = 4096
    orig_slide_spacing = 0.25

    runner = CliRunner()
    savedir = tmp_path / "savedir"
    result = runner.invoke(
        cli,
        [
            "patch",
            "--source",
            str(tiff_image.parent),
            "--save-dir",
            str(savedir),
            "--patch-size",
            str(patch_size),
            "--patch-spacing",
            str(patch_spacing),
        ],
    )
    assert result.exit_code == 0
    stem = tiff_image.stem
    assert (savedir / "masks" / f"{stem}.jpg").exists()
    assert (savedir / "patches" / f"{stem}.h5").exists()
    assert (savedir / "process_list_autogen.csv").exists()
    assert (savedir / "stitches" / f"{stem}.jpg").exists()

    expected_patch_size = round(patch_size * patch_spacing / orig_slide_spacing)
    expected_num_patches = math.ceil(4096 / expected_patch_size) ** 2
    expected_coords = []
    for x in range(0, orig_slide_width, expected_patch_size):
        for y in range(0, orig_slide_height, expected_patch_size):
            expected_coords.append([x, y])
    expected_coords_arr = np.array(expected_coords)

    with h5py.File(savedir / "patches" / f"{stem}.h5") as f:
        assert f["/coords"].attrs["patch_size"] == expected_patch_size
        coords = f["/coords"][()]
    assert coords.shape == (expected_num_patches, 2)
    assert np.array_equal(expected_coords_arr, coords)


# @pytest.mark.parametrize(["model_name", "weights_name"], list_all_models_and_weights())
# def test_jit_compile(model_name: str, weights_name: str):
#     import time

#     from wsinfer._modellib.run_inference import jit_compile

#     w = get_model_weights(model_name, weights_name)
#     size = w.transform.resize_size
#     x = torch.ones(20, 3, size, size, dtype=torch.float32)
#     model = w.load_model()
#     model.eval()
#     NUM_SAMPLES = 1
#     with torch.no_grad():
#         t0 = time.perf_counter()
#         for _ in range(NUM_SAMPLES):
#             out_nojit = model(x).detach().cpu()
#         time_nojit = time.perf_counter() - t0
#     model_nojit = model
#     model = jit_compile(model)  # type: ignore
#     if model is model_nojit:
#         pytest.skip("Failed to compile model (would use original model)")
#     with torch.no_grad():
#         model(x).detach().cpu()  # run it once to compile
#         t0 = time.perf_counter()
#         for _ in range(NUM_SAMPLES):
#             out_jit = model(x).detach().cpu()
#         time_yesjit = time.perf_counter() - t0

#     assert torch.allclose(out_nojit, out_jit)
#     if time_nojit < time_yesjit:
#         pytest.skip(
#             "JIT-compiled model was SLOWER than original: "
#             f"jit={time_yesjit:0.3f} vs nojit={time_nojit:0.3f}"
#         )


def test_issue_89():
    """Do not fail if 'git' is not installed."""
    from wsinfer.cli.infer import _get_info_for_save

    d = _get_info_for_save()
    assert d
    assert "git" in d["runtime"]
    assert d["runtime"]["git"]
    assert d["runtime"]["git"]["git_remote"]
    assert d["runtime"]["git"]["git_branch"]

    # Test that _get_info_for_save does not fail if git is not found.
    orig_path = os.environ["PATH"]
    try:
        os.environ["PATH"] = ""
        d = _get_info_for_save()
        assert d
        assert "git" in d["runtime"]
        assert d["runtime"]["git"] is None
    finally:
        os.environ["PATH"] = orig_path  # reset path


def test_issue_94(tmp_path: Path, tiff_image: Path):
    """Gracefully handle unreadable slides."""

    from wsinfer.cli.cli import cli

    # We have a valid tiff in 'tiff_image.parent'. We put in an unreadable file too.
    badpath = tiff_image.parent / "bad.svs"
    badpath.touch()

    runner = CliRunner()
    results_dir = tmp_path / "inference"
    result = runner.invoke(
        cli,
        [
            "run",
            "--wsi-dir",
            str(tiff_image.parent),
            "--results-dir",
            str(results_dir),
            "--model",
            "breast-tumor-resnet34.tcga-brca",
        ],
    )
    # Important part is that we run through all of the files, despite the unreadble
    # file.
    assert result.exit_code == 0
    assert results_dir.joinpath("model-outputs").joinpath("purple.csv").exists()
    assert not results_dir.joinpath("model-outputs").joinpath("bad.csv").exists()


def test_issue_97(tmp_path: Path, tiff_image: Path):
    """Write a run_metadata file per run."""
    from wsinfer.cli.cli import cli

    runner = CliRunner()
    results_dir = tmp_path / "inference"
    result = runner.invoke(
        cli,
        [
            "run",
            "--wsi-dir",
            str(tiff_image.parent),
            "--results-dir",
            str(results_dir),
            "--model",
            "breast-tumor-resnet34.tcga-brca",
        ],
    )
    assert result.exit_code == 0
    metas = list(results_dir.glob("run_metadata_*.json"))
    assert len(metas) == 1

    time.sleep(2)  # make sure some time has passed so the timestamp is different

    # Run again...
    result = runner.invoke(
        cli,
        [
            "run",
            "--wsi-dir",
            str(tiff_image.parent),
            "--results-dir",
            str(results_dir),
            "--model",
            "breast-tumor-resnet34.tcga-brca",
        ],
    )
    assert result.exit_code == 0
    metas = list(results_dir.glob("run_metadata_*.json"))
    assert len(metas) == 2


def test_issue_125(tmp_path: Path):
    """Test that path in model config can be saved when a pathlib.Path object."""
    from wsinfer.cli.infer import _get_info_for_save
    from wsinfer.modellib.models import get_registered_model

    w = get_registered_model("breast-tumor-resnet34.tcga-brca", torchscript=True)
    w.model_path = Path(w.model_path)  # type: ignore
    info = _get_info_for_save(w)
    with open(tmp_path / "foo.json", "w") as f:
        json.dump(info, f)
