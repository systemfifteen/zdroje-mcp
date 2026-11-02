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
    ["../outside.md", "/etc/passwd", "Projekty/../../outside", ".obsidian/workspace", "C:/Windows/x", ""],
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
