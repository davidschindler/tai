"""Images: tai's read_file shows an image to the model, and images can be attached (an
@-mentioned image file or a pasted screenshot in the editor, /file and -f in the terminal)."""
import base64
import os
import struct
import subprocess
import sys
import zlib

from conftest import TAI, api_log, mock, reset_log


def png(w=8, h=8, rgb=(200, 30, 30)):
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    ch = lambda k, d: struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def image_parts(content):
    return [p for p in content if isinstance(p, dict) and p.get("type") == "image_url"] \
        if isinstance(content, list) else []


def test_read_file_shows_an_image_to_the_model(client, api):
    c = client()
    init(c)
    (c.ws / "plot.png").write_bytes(png())
    reset_log(api)
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "plot.png"}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert not te["error"] and te["summary"].startswith("Read image")
    assert "base64" not in te["output"]            # the editor gets the text, not the data
    tool_msg = [x for x in api_log(api)[-1]["messages"] if x["role"] == "tool"][-1]
    parts = image_parts(tool_msg["content"])
    assert len(parts) == 1 and parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(parts[0]["image_url"]["url"].split(",", 1)[1]) == png()
    # the transcript and the saved session still work
    tr = c.call("transcript")["transcript"]
    tool = [i for i in tr if i["type"] == "tool"][0]
    assert "plot.png" in tool["output"] and not tool["error"]


def test_too_large_image_is_refused(client):
    c = client()
    init(c)
    (c.ws / "big.png").write_bytes(b"\x89PNG" + b"0" * 4_000_000)
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "big.png"}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "too large" in te["output"]


def test_mentioned_image_file_is_attached(client, api):
    c = client()
    init(c)
    (c.ws / "shot.png").write_bytes(png())
    reset_log(api)
    _, end, _ = c.send(mock({"text": "seen"}), attachments=[{"path": "shot.png"}])
    user = api_log(api)[-1]["messages"][-1]
    assert len(image_parts(user["content"])) == 1
    assert "MOCK" in user["content"][0]["text"]


def test_pasted_image_is_sent_and_listed(client, api):
    c = client()
    init(c)
    url = "data:image/png;base64," + base64.b64encode(png()).decode()
    reset_log(api)
    m = c.mark()
    _, end, _ = c.send(mock({"text": "seen"}), images=[{"name": "image 1.png", "data": url}])
    assert end["reason"] == "done"
    user = api_log(api)[-1]["messages"][-1]
    assert image_parts(user["content"])[0]["image_url"]["url"] == url
    assert c.of("user", m)[0]["images"] == ["image 1.png"]
    tr = c.call("transcript")["transcript"]
    u = [i for i in tr if i["type"] == "user"][0]
    assert u["images"] == ["image 1.png"] and u["files"] == []
    # a session with images is saved and loads again
    sid = c.call("state")["session"]["id"]
    c.call("new_session")
    assert [i["type"] for i in c.call("load_session", {"id": sid})["transcript"]] == ["user", "assistant"]


def test_image_only_message(client, api):
    c = client()
    init(c)
    url = "data:image/png;base64," + base64.b64encode(png()).decode()
    r = c.call("send", {"text": "", "images": [{"name": "a.png", "data": url}]})
    assert r["queued"] is False
    end = c.wait("turn_end")
    assert end["reason"] == "done"


def test_bad_pasted_data_is_refused(client):
    c = client()
    init(c)
    r = c.call("send", {"text": "hi", "images": [{"name": "x", "data": "data:text/html;base64,PGI+"}]}, ok=False)
    assert "cannot attach x" in r["error"]["message"]


def test_images_count_little_in_the_context_estimate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tai_img", TAI)
    tai = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tai)
    part = tai.image_part(b"x" * 1_000_000, "image/png")
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, part]}]
    assert tai.msgs_tokens(msgs) < 2000
    shrunk = tai._shrunk(msgs[0])
    assert "image left out" in str(shrunk) and "base64" not in str(shrunk)


def test_terminal_file_option_attaches_an_image(api, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fig.png").write_bytes(png())
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key", "PYTHONIOENCODING": "utf-8",
                "XDG_STATE_HOME": str(tmp_path / "state")})
    reset_log(api)
    r = subprocess.run([sys.executable, TAI, "--raw", "-f", "fig.png", mock({"text": "a red square"})],
                       input="", capture_output=True, text=True, encoding="utf-8", env=env, cwd=ws, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "a red square" in r.stdout
    user = api_log(api)[-1]["messages"][-1]
    assert len(image_parts(user["content"])) == 1
