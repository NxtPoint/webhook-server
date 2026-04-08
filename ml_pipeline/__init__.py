# ML Pipeline for tennis video analysis
#
# Heavy ML imports (torch, opencv, etc.) are NOT loaded at import time.
# Use `from ml_pipeline.pipeline import TennisAnalysisPipeline` explicitly
# when you need the pipeline — this avoids pulling in ML deps on services
# that only need the API blueprint or DB schema.
