import sys, asyncio, json
from pathlib import Path

sys.path.insert(0, "/app")
from idp.config import settings
from tests.vlm_stub import StubServer
from tests.fixtures import make_pdf
from idp.worker import Worker

demo = Path("/tmp/demo")
if demo.exists():
    import shutil
    shutil.rmtree(demo)
demo.mkdir(parents=True)

inp = demo / "input"
out = demo / "output"
inp.mkdir()
out.mkdir()

make_pdf(inp / "example.pdf", pages=3)

stub = StubServer()
stub.__enter__()
settings.vl_endpoints = [stub.endpoint]
settings.vl_endpoint = stub.endpoint
settings.vl_model = "stub-vl"
settings.input_root = inp
settings.output_root = out
settings.artifacts_mode = True
settings.reconstruct_formats = ["docx", "pdf"]

asyncio.run(Worker(artifacts_mode=True, force=True).run())
stub.__exit__(None, None, None)

import shutil
dest = Path("/output")
for item in out.iterdir():
    target = dest / item.name
    if target.exists():
        shutil.rmtree(target) if item.is_dir() else target.unlink()
    shutil.copy2(item, dest / item.name) if item.is_file() else shutil.copytree(item, dest / item.name)

print("Files copied to /output")
print("\n=== Results ===")
for f in sorted(dest.rglob("*")):
    if f.is_file():
        print(f"  {f.relative_to(dest)}  ({f.stat().st_size} bytes)")
