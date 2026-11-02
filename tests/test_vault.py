"""Offline tests for the read-only Obsidian vault tools: sandboxing, exclusion, search, read, links."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zdroje.vault import Vault, VaultError, fold


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    root = tmp_path / "Fifteen"
    (root / "Projekty").mkdir(parents=True)
    (root / "Infraštruktúra").mkdir()
    (root / "Tajné").mkdir()
    (root / ".obsidian").mkdir()
    (root / "AGENTS.md").write_text("# Start pre agentov\nČítaj najprv toto.\n", encoding="utf-8")
    (root / "00 - Prehľad projektov.md").write_text(
        "| [[Projekty/Zdroje MCP\\|Zdroje MCP]] | live |\n- [[Obsidian Sync]]\n", encoding="utf-8"
    )
    (root / "Projekty" / "Zdroje MCP.md").write_text("# Zdroje MCP\nMCP server na kocke.\nPrehľad nástrojov.\n", encoding="utf-8")
    (root / "Infraštruktúra" / "Obsidian Sync.md").write_text("WebDAV na kocke, záloha do Google Drive.\n", encoding="utf-8")
    (root / "Tajné" / "Heslá.md").write_text("kocka tajomstvo\n", encoding="utf-8")
    (root / ".obsidian" / "workspace.md").write_text("kocka\n", encoding="utf-8")
    (root / "obrazok.png").write_bytes(b"\x89PNG")
    outside = tmp_path / "outside.md"
    outside.write_text("kocka mimo vaultu\n", encoding="utf-8")
    os.symlink(outside, root / "Projekty" / "Odkaz von.md")
    return Vault(root, exclude=("Tajné",))


def test_fold_is_case_and_diacritics_insensitive():
    assert fold("Prehľad PROJEKTOV") == "prehlad projektov"
    assert fold("Infraštruktúra") == "infrastruktura"


def test_notes_skip_hidden_excluded_non_md_and_escaping_symlinks(vault: Vault):
    rels = {n.rel for n in vault.notes()}
    assert rels == {
        "AGENTS.md",
        "00 - Prehľad projektov.md",
        "Projekty/Zdroje MCP.md",
        "Infraštruktúra/Obsidian Sync.md",
    }


@pytest.mark.parametrize(
    "bad",
    ["../outside.md", "/etc/passwd", "Projekty/../../outside", ".obsidian/workspace", "C:/Windows/x", "", "Projekty/./Zdroje MCP", "Projekty//Zdroje MCP"],
)
def test_read_rejects_paths_outside_or_hidden(vault: Vault, bad: str):
    with pytest.raises(VaultError) as exc:
        vault.read(bad)
    assert exc.value.code == "bad_path"


def test_read_refuses_excluded_and_symlinked_notes(vault: Vault):
    for ref in ("Tajné/Heslá.md", "Heslá", "Projekty/Odkaz von.md"):
        with pytest.raises(VaultError) as exc:
            vault.read(ref)
        assert exc.value.code == "not_found"


def test_read_by_path_name_and_without_suffix(vault: Vault):
    assert vault.read("Projekty/Zdroje MCP.md")["title"] == "Zdroje MCP"
    assert vault.read("Projekty/Zdroje MCP")["path"] == "Projekty/Zdroje MCP.md"
    assert vault.read("obsidian sync")["path"] == "Infraštruktúra/Obsidian Sync.md"
    assert vault.read("Projekty\\Zdroje MCP.md")["path"] == "Projekty/Zdroje MCP.md"


def test_read_links_and_backlinks_handle_escaped_table_pipes(vault: Vault):
    overview = vault.read("00 - Prehľad projektov")
    assert overview["links"] == ["Obsidian Sync", "Projekty/Zdroje MCP"]
    assert vault.read("Zdroje MCP")["backlinks"] == ["00 - Prehľad projektov.md"]
    assert vault.read("Obsidian Sync")["backlinks"] == ["00 - Prehľad projektov.md"]


def test_read_line_range_and_truncation(vault: Vault):
    part = vault.read("Zdroje MCP", start_line=2, end_line=2)
    assert part["content"] == "MCP server na kocke."
    assert part["lines"] == [2, 2] and part["total_lines"] == 3


def test_search_requires_all_terms_ignores_diacritics_and_hidden(vault: Vault):
    res = vault.search("kocke prehlad")
    assert [h["path"] for h in res["hits"]] == ["Projekty/Zdroje MCP.md"]
    paths = {h["path"] for h in vault.search("kocka")["hits"]}
    assert "Tajné/Heslá.md" not in paths and not any(p.startswith(".obsidian") for p in paths)
    assert {h["path"] for h in vault.search("kocke", folder="Infraštruktúra")["hits"]} == {"Infraštruktúra/Obsidian Sync.md"}
    with pytest.raises(VaultError):
        vault.search("a")


def test_start_returns_start_notes_tree_and_recent(vault: Vault):
    out = vault.start()
    assert [n["path"] for n in out["start_notes"]] == ["AGENTS.md", "00 - Prehľad projektov.md"]
    assert out["note_count"] == 4
    assert {"folder": "Projekty", "notes": ["Zdroje MCP"]} in out["tree"]
    assert len(out["recently_modified"]) == 4


# --- regressions for Hermes review 2026-11-02 (V1-V5) ------------------------------------------------


def _root(v: Vault) -> Path:
    return v.root


def test_v1_internal_file_symlinks_never_served(vault: Vault):
    root = _root(vault)
    (root / ".hidden").mkdir()
    (root / ".hidden" / "secret.md").write_text("HIDDEN\n", encoding="utf-8")
    (root / "raw.txt").write_text("NOT MARKDOWN\n", encoding="utf-8")
    os.symlink("Tajné/Heslá.md", root / "alias.md")            # -> excluded folder
    os.symlink(".hidden/secret.md", root / "alias-hidden.md")  # -> hidden folder
    os.symlink("raw.txt", root / "alias-txt.md")               # -> non-markdown target
    os.symlink("Projekty", root / "LinkDir")                    # symlinked directory
    rels = {n.rel for n in vault.notes()}
    assert not {"alias.md", "alias-hidden.md", "alias-txt.md"} & rels
    assert not any(r.startswith("LinkDir/") for r in rels)
    for ref in ("alias", "alias-hidden", "alias-txt", "LinkDir/Zdroje MCP"):
        with pytest.raises(VaultError) as exc:
            vault.read(ref)
        assert exc.value.code == "not_found"
    for q in ("tajomstvo", "HIDDEN", "MARKDOWN"):
        assert vault.search(q)["total"] == 0


def test_v2_swap_to_symlink_between_listing_and_read_fails(vault: Vault, tmp_path: Path):
    root = _root(vault)
    outside = tmp_path / "outside-secret.md"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    notes = vault.notes()
    note = vault.resolve("Zdroje MCP", notes)
    # file replaced by a symlink after the check
    target = root / "Projekty" / "Zdroje MCP.md"
    target.unlink()
    os.symlink(outside, target)
    from zdroje.vault import Budget
    with pytest.raises(OSError):
        vault._read(note, Budget())
    # parent directory replaced by a symlink after the check
    note2 = vault.resolve("Obsidian Sync", notes)
    (root / "Infraštruktúra").rename(tmp_path / "moved")
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "Obsidian Sync.md").write_text("OUTSIDE\n", encoding="utf-8")
    os.symlink(tmp_path / "evil", root / "Infraštruktúra")
    with pytest.raises(OSError):
        vault._read(note2, Budget())


def test_v3_fifo_and_special_files_do_not_block(vault: Vault):
    os.mkfifo(_root(vault) / "pipe.md")
    assert "pipe.md" not in {n.rel for n in vault.notes()}
    assert vault.search("kocke")["total"] >= 1      # would hang forever if the FIFO were opened
    with pytest.raises(VaultError):
        vault.read("pipe")


def test_v3_read_is_byte_capped_and_query_limited(vault: Vault, monkeypatch):
    import zdroje.vault as vmod
    monkeypatch.setattr(vmod, "MAX_NOTE_BYTES", 1000)
    (_root(vault) / "Velka.md").write_text("x" * 5000 + "\nkoniec\n", encoding="utf-8")
    out = vault.read("Velka")
    assert out["truncated"] is True and "koniec" not in out["content"]
    with pytest.raises(VaultError):
        vault.search("a " * 10 + " ".join(f"slovo{i}" for i in range(20)))
    with pytest.raises(VaultError):
        vault.search("x" * 400)


def test_v3_request_budget_marks_search_incomplete(vault: Vault, monkeypatch):
    from zdroje.vault import Budget
    monkeypatch.setattr("zdroje.vault.Budget", lambda: Budget(remaining=10))
    out = vault.search("kocke")
    assert out.get("incomplete") is True


def test_v4_nfc_nfd_twins_are_ambiguous_not_silently_picked(tmp_path: Path):
    import unicodedata
    root = tmp_path / "V"
    root.mkdir()
    nfc = unicodedata.normalize("NFC", "Prehľad.md")
    nfd = unicodedata.normalize("NFD", "Prehľad.md")
    (root / nfc).write_text("NFC\n", encoding="utf-8")
    (root / nfd).write_text("NFD\n", encoding="utf-8")
    v = Vault(root)
    with pytest.raises(VaultError) as exc:
        v.read("Prehľad")
    assert exc.value.code == "ambiguous"


def test_v4_single_nfd_name_readable_by_nfc_ref(tmp_path: Path):
    import unicodedata
    root = tmp_path / "V"
    (root / unicodedata.normalize("NFD", "Infraštruktúra")).mkdir(parents=True)
    (root / unicodedata.normalize("NFD", "Infraštruktúra") / "Note.md").write_text("ok\n", encoding="utf-8")
    assert Vault(root).read("Infraštruktúra/Note")["content"] == "ok"


def test_v5_os_errors_are_sanitized_at_tool_boundary(vault: Vault, monkeypatch):
    import zdroje.server as srv
    monkeypatch.setattr(srv, "vault", vault)

    def boom():
        raise PermissionError(13, "Permission denied", "/very/secret/absolute/path.md")

    out = srv._vault_call(boom)
    assert out["error"] == "io_error" and "/very" not in out["message"]
    denied = _root(vault) / "Projekty" / "Zdroje MCP.md"
    denied.chmod(0)
    try:
        if os.geteuid() != 0:
            res = srv._vault_call(lambda: vault.read("Zdroje MCP"))
            assert res["error"] == "io_error" and str(_root(vault)) not in str(res)
    finally:
        denied.chmod(0o644)
