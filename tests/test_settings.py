from pathlib import Path

from orrery import paths, settings, store


def test_default_root_is_local_share(monkeypatch):
    monkeypatch.delenv("ORRERY_HOME", raising=False)
    assert paths.data_root() == Path.home() / ".local" / "share" / "orrery"


def test_template_loads_as_defaults(orrery_home):
    (orrery_home / "config.toml").write_text(settings.CONFIG_TEMPLATE)
    assert settings.load_settings() == settings.Settings(root=orrery_home)


def test_load_settings_reads_values(orrery_home):
    (orrery_home / "config.toml").write_text(
        'day_boundary_hour = 4\n[ai]\nmodel = "opus"\nclaude_path = "/x/claude"\n'
        '[seed]\nsubjects = [["math", "Math"]]\nkinds = ["learn"]\nsession_cap = 3\n'
    )
    s = settings.load_settings()
    assert (s.day_boundary_hour, s.ai_model, s.claude_path) == (4, "opus", "/x/claude")
    assert (s.seed_subjects, s.seed_kinds, s.seed_cap) == ((("math", "Math"),), ("learn",), 3)


def test_missing_root_or_config_is_unavailable(tmp_path):
    assert isinstance(settings.load_settings(tmp_path / "nope"), store.Unavailable)
    (tmp_path / "r").mkdir()
    assert isinstance(settings.load_settings(tmp_path / "r"), store.Unavailable)


def test_bad_values_are_unavailable(orrery_home):
    (orrery_home / "config.toml").write_text("day_boundary_hour = 30\n")
    assert isinstance(settings.load_settings(), store.Unavailable)
    # the app's mount reads settings on its own thread: a bad section is a line, not a crash
    for text in ("ai = 3\n", 'seed = "x"\n'):
        (orrery_home / "config.toml").write_text(text)
        assert isinstance(settings.load_settings(), store.Unavailable), text


def test_theme_is_read_and_never_makes_settings_unavailable(orrery_home):
    """Validated against the app's themes at mount, not here: settings.py stays textual-free."""
    cfg = orrery_home / "config.toml"
    cfg.write_text('theme = "nord"\n' + settings.CONFIG_TEMPLATE)
    assert settings.load_settings().theme == "nord"
    for line in ('theme = ""', "theme = 3", "theme = []"):
        cfg.write_text(line + "\n" + settings.CONFIG_TEMPLATE)
        got = settings.load_settings()
        assert isinstance(got, settings.Settings), (line, got)


def test_update_config_inserts_theme_above_first_table_and_round_trips(orrery_home):
    """A scalar after `[ai]` would be a field of that table, still parsing: so above it."""
    cfg = orrery_home / "config.toml"
    cfg.write_text(settings.CONFIG_TEMPLATE.replace('model = "sonnet"', 'model = "opus"  # mine'))
    settings.update_config(cfg, {"theme": "nord"})
    text = cfg.read_text()
    lines = text.splitlines()
    assert lines.index('theme = "nord"') < lines.index("[ai]")
    assert 'model = "opus"  # mine' in lines  # the rest of the file, comments too, survives
    s = settings.load_settings()
    assert (s.theme, s.ai_model, s.day_boundary_hour) == ("nord", "opus", 5)

    settings.update_config(cfg, {"theme": "tokyo-night"})
    assert settings.load_settings().theme == "tokyo-night"
    assert cfg.read_text() == text.replace('"nord"', '"tokyo-night"')  # replaced in place
    assert not list(paths.backup_dir().iterdir())  # backup=False: a keystroke, not an edit


def test_update_config_quotes_and_writes_a_missing_file(orrery_home):
    cfg = orrery_home / "config.toml"
    settings.update_config(cfg, {"theme": 'a"b\\c'})
    assert cfg.read_text() == 'theme = "a\\"b\\\\c"\n'
    assert settings.load_settings().theme == 'a"b\\c'


def test_update_config_keeps_unicode_line_breaks_inside_strings(orrery_home):
    """U+2028, U+2029 and U+0085 may sit inside a TOML string; split there, the string would
    be cut in two and the file would no longer parse."""
    cfg = orrery_home / "config.toml"
    odd = "/x/a\u2028b\u2029c\u0085d"
    cfg.write_text(f'day_boundary_hour = 5\n\n[ai]\nclaude_path = "{odd}"\n')
    settings.update_config(cfg, {"theme": "nord"})
    s = settings.load_settings()
    assert (s.theme, s.claude_path) == ("nord", odd)


def test_update_config_leaves_a_matching_key_inside_a_table_alone(orrery_home):
    cfg = orrery_home / "config.toml"
    cfg.write_text('day_boundary_hour = 5\n\n[ai]\ntheme = "a field of [ai]"\n')
    settings.update_config(cfg, {"theme": "nord"})
    assert cfg.read_text() == (
        'day_boundary_hour = 5\n\ntheme = "nord"\n\n[ai]\ntheme = "a field of [ai]"\n'
    )


def test_update_config_writes_through_a_symlink(orrery_home, tmp_path):
    """A dotfile manager's link stays a link, and the file it points at gets the theme."""
    target = tmp_path / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_text(settings.CONFIG_TEMPLATE)
    link = orrery_home / "config.toml"
    link.symlink_to(target)
    settings.update_config(link, {"theme": "nord"})
    assert link.is_symlink() and link.resolve() == target.resolve()
    assert settings.load_settings().theme == "nord"
    assert 'theme = "nord"' in target.read_text().splitlines()
