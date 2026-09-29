# UCI Iris real-data case

Run `python examples/iris_case/fetch.py` once to opt in to the network download. The helper requires the raw historical UCI source bytes to match SHA-256 `6f608b71a7317216319b4d27b4d9bc84e6abd734eda7872b71a458569e2656c0` before writing a headered CSV. The downloaded data is ignored by Git; subsequent pipeline runs are offline.

Then run:

```sh
python -m reproforge run examples/iris_case/project.json
python -m reproforge verify examples/iris_case/project.json RUN_ID
```

The pipeline gates CSV quality, trains a simple nearest-centroid baseline on the first 40 rows of each species, evaluates on the last 10 rows per species, and writes a report. This split is a deterministic demonstration, **not** a representative model-quality claim or a temporal leakage test. Source: R. A. Fisher's Iris dataset via the [UCI Machine Learning Repository](https://archive.ics.uci.edu/dataset/53/iris). Cite the repository if reusing the data. The example makes an explicit external download only in `fetch.py`; ReproForge itself still runs offline.
