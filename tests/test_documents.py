"""Word, Excel, PowerPoint and Stata files and Jupyter notebooks: read_file and attachments
show them as text (notebook plots as images), and edit_notebook changes notebook cells.
The files in fixtures/ were made with python-docx, openpyxl, python-pptx and pandas."""
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import TAI, api_log, mock, reset_log

FIX = Path(__file__).resolve().parent / "fixtures"
spec = importlib.util.spec_from_file_location("tai_docs", TAI)
tai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tai)


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def text(path):
    return tai.document_text(Path(path).resolve())


# ------------------------------------------------------------------ the readers

def test_word():
    t = text(FIX / "report.docx")
    assert t.splitlines()[:4] == ["# Referee report", "# Main comments",
                                  "The identification strategy is unclear.", "- First point"]
    assert "| Model | Estimate |" in t and "| OLS | 0.42 |" in t and t.rstrip().endswith("End.")


def test_excel():
    t = text(FIX / "macro.xlsx")
    assert '## Sheet "Data"' in t and '## Sheet "Notes"' in t
    assert "1: country | year | gdp | date" in t
    assert "2: NL | 2024 | 1100000000000 | 2024-03-01" in t
    assert "3: BE | 2024 | 630000000000 | 2024-05-02 14:30" in t
    assert "4:  |  | =SUM(C2:C3)" in t                 # a formula without a saved value
    assert "6:  |  |  |  |  | far \\| away" in t
    assert "1: Source: World Bank" in t


def test_powerpoint():
    t = text(FIX / "talk.pptx")
    assert "2 slides" in t
    assert "## Slide 1\nResults\nEffect is 0.42\nNotes: Mention the robustness checks" in t
    assert "## Slide 2\nThanks" in t


@pytest.mark.parametrize("version", [114, 117, 118, 119])
def test_stata(version):
    t = text(FIX / f"survey{version}.dta")
    assert f"format {version}" in t and "3 observations, 5 variables" in t
    assert "Data label: Household survey 2026" in t
    assert "  id · long · %12.0g · Person id" in t
    assert "  sex · byte · %8.0g · - · sex: 0 = Female, 1 = Male" in t
    assert "  2 | Female | . | Utrecht | 51" in t     # value label applied, missing value shown as .
    city = "Unna" if version == 114 else "Ünnaström"
    assert f"  3 | Male | 1800.25 | {city} | 27" in t


def test_stata_long_strings():
    t = text(FIX / "strl118.dta")
    assert "note · strL" in t
    rows = [l for l in t.splitlines() if l.startswith("  1 | Male")]
    assert rows and "a long note a long note" in rows[0] and rows[0].endswith("…")
    assert "  2 | Female | . | Utrecht | 51 | short" in t


def test_stata_dates(tmp_path):
    """A %td variable is shown as dates (made by hand: format 118 with one double)."""
    import struct
    K, N = 1, 2
    def sec(name, body):
        return f"<{name}>".encode() + body + f"</{name}>".encode()
    head = (b"<stata_dta>" + sec("header", sec("release", b"118") + sec("byteorder", b"LSF")
            + sec("K", struct.pack("<H", K)) + sec("N", struct.pack("<Q", N))
            + sec("label", struct.pack("<H", 0)) + sec("timestamp", b"\0")))
    parts = [("variable_types", struct.pack("<H", 65526)), ("varnames", b"day".ljust(129, b"\0")),
             ("sortlist", b"\0" * 4), ("formats", b"%td".ljust(57, b"\0")),
             ("value_label_names", b"\0" * 129), ("variable_labels", b"\0" * 321),
             ("characteristics", b""), ("data", struct.pack("<dd", 0, 23376)), ("strls", b""),
             ("value_labels", b"")]
    body, offs, pos = b"", [], len(head) + len("<map>") + 112 + len("</map>")
    for name, b in parts:
        offs.append(pos + len(body))
        body += sec(name, b)
    end = pos + len(body)
    the_map = [0, len(head)] + offs + [end, end + len("</stata_dta>")]
    data = head + sec("map", struct.pack("<14Q", *the_map)) + body + b"</stata_dta>"
    (tmp_path / "d.dta").write_bytes(data)
    t = text(tmp_path / "d.dta")
    assert "  1960-01-01" in t and "  2024-01-01" in t


def test_damaged_files_give_a_clear_error(tmp_path):
    for name in ("x.docx", "x.xlsx", "x.pptx"):
        (tmp_path / name).write_bytes(b"not a zip file")
        with pytest.raises(tai.ToolError, match="not a valid"):
            text(tmp_path / name)
    (tmp_path / "x.dta").write_bytes(b"\x01\x02junk" * 10)
    with pytest.raises(tai.ToolError, match="not a Stata data file"):
        text(tmp_path / "x.dta")


# ------------------------------------------------------------------ notebooks

PNG = (FIX.parent.parent / "vscode" / "media" / "tai-tab-dark.png").read_bytes()


def notebook(path, minor=5):
    nb = {"cells": [
        {"cell_type": "markdown", "id": "m1", "metadata": {}, "source": ["# Analysis\n", "Intro"]},
        {"cell_type": "code", "execution_count": 3, "id": "c1", "metadata": {}, "source": ["print('hi')\n", "plot()"],
         "outputs": [{"name": "stdout", "output_type": "stream", "text": ["hi\n"]},
                     {"data": {"image/png": base64.b64encode(PNG).decode() + "\n", "text/plain": ["<Figure>"]},
                      "metadata": {}, "output_type": "display_data"},
                     {"ename": "ValueError", "evalue": "bad", "output_type": "error",
                      "traceback": ["\x1b[0;31mValueError\x1b[0m: bad"]}]}],
          "metadata": {"kernelspec": {"language": "python", "name": "python3", "display_name": "Python 3"}},
          "nbformat": 4, "nbformat_minor": minor}
    text = json.dumps(nb, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def test_read_notebook_with_outputs_and_plot(client, api):
    c = client()
    init(c)
    notebook(c.ws / "a.ipynb")
    reset_log(api)
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "a.ipynb"}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert not te["error"] and "1 images" in te["summary"]
    assert "──── cell 2 · code · [3]" in te["output"] and "ValueError: bad" in te["output"]
    assert "\x1b" not in te["output"] and "base64" not in te["output"]
    tool_msg = [x for x in api_log(api)[-1]["messages"] if x["role"] == "tool"][-1]
    parts = tool_msg["content"]
    assert parts[1]["text"] == "[image 1, output of cell 2]:"
    assert base64.b64decode(parts[2]["image_url"]["url"].split(",", 1)[1]) == PNG


def test_edit_notebook_cells(client, api):
    c = client()
    init(c)
    orig = notebook(c.ws / "a.ipynb")
    seen = []
    c.approve = lambda p: seen.append(p) or {"decision": "yes"}
    ed = lambda **a: {"name": "edit_notebook", "args": {"path": "a.ipynb", **a}}
    _, end, m = c.send(mock({"tools": [ed(cell=2, action="replace", source="print('bye')")]},
                            {"tools": [ed(cell=0, action="insert", source="import pandas as pd")]},
                            {"tools": [ed(cell=2, action="replace", source="# Title", cell_type="markdown")]},
                            {"tools": [ed(cell=9, action="delete")]},
                            {"tools": [{"name": "edit_file", "args": {"path": "a.ipynb", "old_string": "x",
                                                                      "new_string": "y"}}]},
                            {"text": "k"}))
    outs = c.of("tool_end", m)
    assert [o["error"] for o in outs] == [False, False, False, True, True]
    assert "there is no cell 9" in outs[3]["output"] and "edit_notebook" in outs[4]["output"]
    assert len(seen) == 3 and seen[0]["detail"]["old"] == orig     # the editor gets the whole file
    nb = json.loads((c.ws / "a.ipynb").read_text(encoding="utf-8"))
    assert [x["cell_type"] for x in nb["cells"]] == ["code", "markdown", "code"]
    assert nb["cells"][0]["source"] == ["import pandas as pd"] and len(nb["cells"][0]["id"]) == 8
    assert nb["cells"][1] == {"cell_type": "markdown", "id": "m1", "metadata": {}, "source": ["# Title"]}
    assert nb["cells"][2]["outputs"] == [] and nb["cells"][2]["execution_count"] is None
    assert nb["cells"][2]["id"] == "c1"
    # saved like Jupyter does (one-space indent, sorted keys), so a diff shows only the change
    assert (c.ws / "a.ipynb").read_text(encoding="utf-8") == json.dumps(nb, indent=1, sort_keys=True,
                                                                         ensure_ascii=False) + "\n"
    # /rewind puts the notebook back
    idx = [i for i in c.call("transcript")["transcript"] if i["type"] == "user"][0]["index"]
    assert [f["rel"] for f in c.call("rewind_preview", {"index": idx})["files"]] == ["a.ipynb"]
    c.call("rewind", {"index": idx})
    assert (c.ws / "a.ipynb").read_text(encoding="utf-8") == orig


def test_old_notebook_cells_get_no_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    notebook(tmp_path / "b.ipynb", minor=4)
    tai.tool_edit_notebook({"path": "b.ipynb", "cell": 0, "action": "insert", "source": "x = 1",
                            "cell_type": "code"}, tai.Permissions("auto"))
    nb = json.loads((tmp_path / "b.ipynb").read_text(encoding="utf-8"))
    assert "id" not in nb["cells"][0] and nb["cells"][0]["outputs"] == []


def test_plain_json_notebook_keeps_its_layout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    nb = {"nbformat": 4, "nbformat_minor": 2, "metadata": {}, "cells": [
        {"cell_type": "code", "metadata": {}, "source": "a = 1", "outputs": [], "execution_count": None}]}
    (tmp_path / "c.ipynb").write_text(json.dumps(nb, indent=2))
    tai.tool_edit_notebook({"path": "c.ipynb", "cell": 1, "action": "replace", "source": "a = 2"},
                           tai.Permissions("auto"))
    nb["cells"][0]["source"] = ["a = 2"]
    assert (tmp_path / "c.ipynb").read_text() == json.dumps(nb, indent=2, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------ attaching and reading

def test_read_file_and_attachments_of_office_files(client, api):
    c = client()
    init(c)
    for f in ("report.docx", "macro.xlsx", "survey118.dta"):
        shutil.copy(FIX / f, c.ws / f)
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "macro.xlsx", "offset": 3,
                                                                       "limit": 3}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert "1: country | year" in te["output"] and "Excel workbook" not in te["output"]
    reset_log(api)
    c.send(mock({"text": "seen"}), attachments=[{"path": "report.docx"}, {"path": "survey118.dta"}])
    user = api_log(api)[-1]["messages"][-1]["content"]
    user = user if isinstance(user, str) else tai.text_of(user)
    assert "# Referee report" in user and "Person id" in user


def test_terminal_file_option_attaches_a_word_file(api, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    shutil.copy(FIX / "report.docx", ws / "report.docx")
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key", "PYTHONIOENCODING": "utf-8",
                "XDG_STATE_HOME": str(tmp_path / "state")})
    reset_log(api)
    r = subprocess.run([sys.executable, TAI, "--raw", "-f", "report.docx", mock({"text": "ok"})],
                       input="", capture_output=True, text=True, encoding="utf-8", env=env, cwd=ws, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "The identification strategy is unclear." in tai.text_of(api_log(api)[-1]["messages"][-1]["content"])
