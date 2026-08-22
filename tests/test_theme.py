"""Tests for the theme values the app loads at startup.

``THEME_TOKENS`` in main.py holds one color per name for the light theme and the
dark theme, and the font sizes come from the system font. ``theme.qss.template``
refers to both sets of names, and ``_apply_theme`` substitutes them in to build the
Qt stylesheet before the first window is shown.
"""

from __future__ import annotations

from string import Template

import pytest

from irods_client_system_tray.main import THEME_TEMPLATE_PATH, THEME_TOKENS, _font_size_tokens_from_system_font

THEMES = sorted(THEME_TOKENS)


@pytest.mark.parametrize("theme", THEMES)
def test_stylesheet_builds_for_every_theme(theme, qapp):
    # The app fills in the stylesheet this way when it starts. If the stylesheet asks
    # for a value that nothing supplies, this step fails and the app never gets as far
    # as opening a window. The template and the values are edited separately, so adding
    # a rule without adding its value is easy to do.
    substitutions = {**THEME_TOKENS[theme], **_font_size_tokens_from_system_font(qapp)}

    rendered = Template(THEME_TEMPLATE_PATH.read_text()).substitute(substitutions)

    assert "$" not in rendered
