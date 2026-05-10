import os
import uvicorn


if __name__ == "__main__":
    # 2026-05-07 — tracemalloc disabled in prod (overhead). Re-enable manually
    # for debugging by uncommenting + setting env var.
    if os.environ.get("POLYORACLE_TRACEMALLOC") == "1":
        import tracemalloc
        tracemalloc.start(25)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
