"""Entry point so `python -m ml_pipeline.diag.bench_silver` works the same
as `python -m ml_pipeline.diag.bench_silver.bench`."""
from ml_pipeline.diag.bench_silver.bench import main

if __name__ == "__main__":
    import sys
    sys.exit(main())
