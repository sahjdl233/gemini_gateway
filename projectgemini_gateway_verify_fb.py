import sys, os
sys.path.insert(0, ".")
os.environ["FIREBASE_01_API_KEY"] = "AIzaSyTESTAPIKEY"
os.environ["FIREBASE_01_APP_ID"] = "1:12345:web:abc123"
os.environ["FIREBASE_01_DEBUG_TOKEN"] = "debug-token-0000"
from config.loader import load_config
from app.main import build_runtime
from protocol.openai import parse_openai_chat_request
from tests.providers._firebase_fakes import FakeHttp, FakeResponse, make_sse
import asyncio
cfg = load_config("config.yaml.example")
scheduler = build_runtime(cfg)
provider = scheduler.providers["firebase"]
async def test_complete():
    fake = FakeHttp()
    fake.responses.append(fake.exchange_ok())
    fake.responses.append(FakeResponse(200, json_body={"candidates":[{"content":{"role":"model","parts":[{"text":"Hello from Firebase!"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":4,"thoughtsTokenCount":0,"totalTokenCount":7}}))
    provider.set_http_client(fake)
    req = parse_openai_chat_request({"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]})
    resp = await scheduler.chat_completion(req)
    print(f"complete: text={resp.text!r} finish={resp.finish_reason} tokens={resp.usage.total_tokens}")
async def test_stream():
    fake = FakeHttp()
    fake.responses.append(fake.exchange_ok())
    fake.stream_responses.append(FakeResponse(200, sse_chunks=[make_sse({"candidates":[{"content":{"role":"model","parts":[{"text":"streaming "}]}}]},{"candidates":[{"content":{"role":"model","parts":[{"text":"works"}]}}]},{"candidates":[{"content":{"role":"model","parts":[{"text":""}]},"finishReason":"STOP"}],"usageMetadata":{"totalTokenCount":7}},"[DONE]")]))
    provider.set_http_client(fake)
    req = parse_openai_chat_request({"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}],"stream":True})
    chunks = [c async for c in scheduler.stream_chat(req)]
    texts = [c.text for c in chunks if c.text is not None]
    print(f"stream: chunks={texts} finish={chunks[-1].finish_reason}")
asyncio.run(test_complete())
asyncio.run(test_stream())
print("end-to-end: OK")
