#!/usr/bin/env python3
"""
Browser-less UI test for bridge_web_telnet, using NiceGUI's own User fixture.

What this covers that test_bridge_core_sim.py cannot: the parts that only exist
once a CLIENT is connected - every tab actually building, and above all the
register groups, which are built lazily when their tab is first opened. That
laziness is not a nicety: building all 183 registers up front put ~8600 active
bindings on the page, which NiceGUI re-evaluates ten times a second. Without a
test, "does clicking a group tab actually fill it?" would only ever be answered by
opening a browser.

    python -m pytest scripts/test_web_ui.py -q \
        -p nicegui.testing.user_plugin -o asyncio_mode=auto -o main_file=

All three switches are needed and none has a default that works here:
  -p ...user_plugin   brings the `user` fixture in without a conftest.py. A
                      `pytest_plugins = [...]` line in this file would be refused,
                      because pytest 8 only accepts that in a rootdir conftest.
  asyncio_mode=auto   without it pytest skips every async test below with a
                      confusing "async def functions are not natively supported".
  main_file=          NiceGUI otherwise looks for a `main.py` relative to a
                      pytest.ini and asserts that the ini exists. Emptying the
                      setting makes it skip that lookup, and the `web` fixture
                      below registers the page itself.

They are command-line options rather than a pytest.ini because that ini would be
the first one this repo has and would then silently govern anything added later.

Not part of test_bridge_core_sim.py on purpose: that one is stdlib-only and runs
anywhere, this one needs the test framework.
"""

import sys
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import User

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def web(user: User):
    """The front end module, with its page registered for THIS test.

    Depends on `user` so it runs after it: the user fixture resets NiceGUI's
    globals, which drops every registered page. The module itself is imported once
    and cached like any other, so the @ui.page decorator only ever ran once - hence
    the explicit re-registration here, without which every test after the first
    would open a 404.
    """
    import bridge_web_telnet
    ui.page("/")(bridge_web_telnet.index)
    return bridge_web_telnet


async def test_page_opens_with_every_tab(user: User, web) -> None:
    await user.open("/")
    for label in ("Bridge Parameters", "LAN8651 Registers", "Test Modes",
                  "Terminal", "Certificates", "MQTT", "Help"):
        await user.should_see(label)


async def test_connection_bar_starts_disconnected(user: User, web) -> None:
    await user.open("/")
    await user.should_see("not connected")
    # The identity line is the one that says whether the values below mean
    # anything - it must be there before anyone connects.
    await user.should_see("not read yet")


async def test_register_groups_build_lazily(user: User, web) -> None:
    """The first group is there on load; the others appear when their tab is opened.

    0x00000000 is the first register of MMS0, which builds eagerly. The MMS10 group
    is the last tab, so if its registers are present before it is clicked, the lazy
    build is not actually lazy - and if they are still absent after, it is broken.
    """
    await user.open("/")
    groups = list(web.session.reg_categories)
    first_addr = web.session.reg_categories[groups[0]][0]
    last_group = groups[-1]
    last_addr = web.session.reg_categories[last_group][0]

    await user.should_see(first_addr)
    await user.should_not_see(last_addr)

    user.find(last_group).click()
    await user.should_see(last_addr)


async def test_bitfields_decode_from_the_value(user: User, web) -> None:
    """The decoded bit fields are bound to the value field, so a value arriving
    from the device (or typed in) shows up decoded without a redraw."""
    await user.open("/")
    addr = "0x000308FB"        # T1STSTCTL - the test-mode register, has bit fields
    assert addr in web.session.reg_meta, "model changed - pick another register here"
    detail = web.register_detail_html(web.session.reg_meta[addr], "0x0283A1")
    assert "[15:13]" in detail
    assert "= 4" in detail, detail
    # And nothing at all when no value has been read: a 0 must never be mistaken
    # for a measurement.
    assert "= " not in web.register_detail_html(web.session.reg_meta[addr], "")


async def test_actions_without_a_link_do_not_crash(user: User, web) -> None:
    """Every board action must refuse politely while nothing is connected, rather
    than raising - the buttons are reachable the moment the page loads."""
    await user.open("/")
    web.read_environment()
    web.read_register("0x00000000")
    web.bulk_read_registers()
    web.memory_overview()
    web.run_cmd("stats")
    await user.should_see("Press Connect first")
