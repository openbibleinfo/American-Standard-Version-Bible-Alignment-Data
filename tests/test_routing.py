"""What a route string means, and what it refuses to mean.

This is the only thing between a typed flag and a paid run, so every case here
is one a chapter would otherwise have discovered after fetching its sources and
preparing its packets. Nothing spawns or sends: parsing is pure.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from alignment_research.chapter import Halt
from alignment_research.providers import CREDENTIALS
from alignment_research.driver import (
    CODEX_BILLING_VARS,
    clis_in_use,
    codex_billing_is_the_subscription,
    ensure_routes_usable,
    resume_command,
    session_resume,
)
from alignment_research.routing import (
    PACK_ALL,
    PACK_ALL_CAP,
    PACK_DEFAULT,
    PACK_LIMIT,
    CHAPTER_PASSES,
    DEFAULT_ROUTES,
    Route,
    parse_route,
    resolve_routes,
)


class RouteParsingTests(unittest.TestCase):
    def test_a_bare_provider_is_a_complete_route(self):
        self.assertEqual(parse_route("openai"), Route("openai"))
        self.assertEqual(parse_route("code"), Route("code"))

    def test_every_part_can_be_said_out_loud(self):
        route = parse_route("openai/gpt-5.6-sol:batch@medium")
        self.assertEqual(
            route, Route("openai", "gpt-5.6-sol", batch=True, effort="medium")
        )

    def test_a_model_name_implies_its_endpoint(self):
        """Typing the endpoint twice is the part that gets typed wrongly."""
        self.assertEqual(
            parse_route("gemini-3.7-flash"),
            Route("gemini", "gemini-3.7-flash"),
        )

    def test_sol_and_terra_are_short_openai_model_routes(self):
        self.assertEqual(parse_route("sol"), Route("openai", "gpt-5.6-sol"))
        self.assertEqual(parse_route("terra"), Route("openai", "gpt-5.6-terra"))
        self.assertEqual(
            parse_route("sol:batch"),
            Route("openai", "gpt-5.6-sol", batch=True),
        )
        self.assertEqual(
            parse_route("terra:batch"),
            Route("openai", "gpt-5.6-terra", batch=True),
        )
        self.assertEqual(
            parse_route("gpt-5.6-terra"), Route("openai", "gpt-5.6-terra")
        )

    def test_a_route_round_trips_through_its_own_spelling(self):
        for spec in (
            "code", "code@high", "openai", "gemini",
            "anthropic/claude-opus-5:batch@low",
        ):
            self.assertEqual(parse_route(spec).spec(), spec)

    def test_the_cli_and_the_api_are_spelled_apart(self):
        """They bill an order of magnitude apart, so they cannot share a name."""
        self.assertTrue(parse_route("claude-code").is_code)
        self.assertFalse(parse_route("claude").is_code)
        self.assertEqual(parse_route("claude").provider, "anthropic")

    def test_a_route_names_the_model_a_report_will_state(self):
        self.assertEqual(parse_route("gemini").name, "gemini-3.7-flash")
        self.assertEqual(parse_route("code").name, "claude-code")


class RouteRefusalTests(unittest.TestCase):
    """Every refusal here is one that would otherwise cost a prepared chapter."""

    def test_an_unknown_provider_is_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("mistral")
        self.assertIn("mistral", str(caught.exception))

    def test_an_unknown_effort_is_refused_before_anything_is_spent(self):
        with self.assertRaises(ValueError):
            parse_route("openai@maximum")

    def test_minimal_is_not_an_effort_because_gemini_rejects_it(self):
        with self.assertRaises(ValueError):
            parse_route("gemini@minimal")

    def test_gemini_refuses_a_batch_route(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("gemini:batch")
        self.assertIn("no batch endpoint", str(caught.exception))

    def test_there_is_no_batch_endpoint_behind_a_claude_code_session(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("code:batch")
        self.assertIn("anthropic:batch", str(caught.exception))

    def test_the_code_route_does_not_take_a_model(self):
        with self.assertRaises(ValueError):
            parse_route("code/claude-opus-5")

    def test_an_unknown_modifier_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            parse_route("openai:async")

    def test_an_empty_route_is_refused(self):
        with self.assertRaises(ValueError):
            parse_route("  ")


class ToolFreeRouteTests(unittest.TestCase):
    """Which routes return a JSON object, and which one runs a session.

    What this decides is who writes the accepted file. On a tool-free route the
    parent validates the response and lands it, so a model that misremembers a
    path cannot leave a chapter looking finished; on a session route the model
    writes it and the parent reads the filesystem to find out whether it did.
    """

    def test_a_verse_pass_is_tool_free_wherever_it_can_be(self):
        for spec in ("code", "openai", "gemini", "anthropic", "sol:batch"):
            self.assertTrue(parse_route(spec).tool_free, spec)

    def test_the_session_route_has_to_be_asked_for(self):
        route = parse_route("code:session")
        self.assertFalse(route.tool_free)
        self.assertTrue(route.session)
        self.assertTrue(route.is_code)
        self.assertEqual(route.spec(), "code:session")
        self.assertIn("session", route.describe())

    def test_codex_runs_a_session_because_it_has_no_other_shape(self):
        """No flag there empties the tool set or holds a reply to a schema."""
        self.assertFalse(parse_route("codex").tool_free)
        self.assertFalse(parse_route("codex/terra").tool_free)

    def test_only_code_has_two_shapes_to_choose_between(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("sol:session")
        self.assertIn("no session route", str(caught.exception))

    def test_an_effort_still_rides_alongside_the_modifier(self):
        route = parse_route("code:session@high")
        self.assertTrue(route.session)
        self.assertEqual(route.effort, "high")
        self.assertEqual(route.spec(), "code:session@high")

    def test_the_default_verse_routes_are_tool_free(self):
        routes = resolve_routes()
        self.assertTrue(routes["packet"].tool_free)
        self.assertTrue(routes["review"].tool_free)


class PackedRouteTests(unittest.TestCase):
    """How many verses share one request, said in the route that ran them.

    It is spelled here rather than as a driver flag because a route round-trips
    into the run report, and "eight verses answered this request" is exactly the
    kind of fact a finished chapter has to be able to state about itself.
    """

    def test_an_unstated_route_has_not_decided_yet_and_resolve_decides(self):
        """`pack is None` is "nobody said", which is not "one verse".

        Reading it as one made every unresolved `code:session` and `codex`
        route tool-free, since neither is packed and both were being asked the
        wrong question. The decision belongs to `resolve_routes`, which is the
        only place that knows which pass a route is for.
        """
        route = parse_route("code")
        self.assertIsNone(route.pack)
        self.assertFalse(route.packed)
        self.assertTrue(route.packs_by_default)

    def test_a_verse_pass_packs_by_default_and_a_chapter_pass_has_nothing_to(self):
        routes = resolve_routes()
        self.assertEqual(routes["packet"].pack, PACK_ALL)
        self.assertEqual(routes["review"].pack, PACK_ALL)
        self.assertFalse(routes["reconcile"].packed)
        self.assertFalse(routes["decisions"].packed)

    def test_unpacked_is_how_a_verse_pass_asks_for_the_older_shape(self):
        routes = resolve_routes(align="code:unpacked")
        self.assertEqual(routes["packet"].pack, 1)
        self.assertFalse(routes["packet"].packed)
        self.assertEqual(routes["packet"].spec(), "code:unpacked")
        # Either CLI packs by default, so `codex` needs the word too, and the
        # agentic per-verse route it used to name is `codex:unpacked`.
        self.assertEqual(
            resolve_routes(align="codex")["packet"].spec(), "codex:packed=all",
        )
        self.assertEqual(
            resolve_routes(align="codex:unpacked")["packet"].spec(),
            "codex:unpacked",
        )
        self.assertFalse(parse_route("codex:unpacked").tool_free)
        # A provider route packs by default too, now that one can.
        self.assertEqual(resolve_routes(align="sol")["packet"].spec(),
                         "openai/gpt-5.6-sol:packed=all")
        self.assertEqual(resolve_routes(align="sol:unpacked")["packet"].spec(),
                         "openai/gpt-5.6-sol:unpacked")
        self.assertEqual(resolve_routes()["reconcile"].spec(), "code")

    def test_a_size_may_be_stated_and_round_trips(self):
        route = parse_route("code:packed=4")
        self.assertEqual(route.pack, 4)
        self.assertTrue(route.packed)
        self.assertEqual(route.spec(), "code:packed=4")
        self.assertIn("4 verses per request", route.describe())

    def test_an_unstated_size_takes_the_default_and_still_says_so(self):
        route = parse_route("code:packed")
        self.assertEqual(route.pack, PACK_DEFAULT)
        self.assertEqual(route.spec(), f"code:packed={PACK_DEFAULT}")

    def test_an_effort_still_rides_alongside_a_pack(self):
        route = parse_route("code:packed=6@high")
        self.assertEqual((route.pack, route.effort), (6, "high"))
        self.assertEqual(route.spec(), "code:packed=6@high")

    def test_all_is_a_size_and_the_ceiling_does_not_apply_to_it(self):
        """Asking for the whole chapter is a decision, not a typo."""
        route = parse_route("code:packed=all")
        self.assertEqual(route.pack, PACK_ALL)
        self.assertTrue(route.packed)
        self.assertEqual(route.spec(), "code:packed=all")
        self.assertIn(
            f"every verse, in requests of at most {PACK_ALL_CAP}",
            route.describe(),
        )
        self.assertEqual(parse_route("code:packed=all@high").spec(), "code:packed=all@high")

    def test_a_word_that_is_not_all_is_still_a_typo(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("code:packed=every")
        self.assertIn("or `all`", str(caught.exception))

    def test_a_pack_of_one_is_refused_rather_than_read_as_the_plain_route(self):
        """Otherwise a run reports packing that never happened."""
        with self.assertRaises(ValueError) as caught:
            parse_route("code:packed=1")
        self.assertIn("at least two verses", str(caught.exception))

    def test_a_pack_past_the_ceiling_is_refused_before_it_is_paid_for(self):
        with self.assertRaises(ValueError) as caught:
            parse_route(f"code:packed={PACK_LIMIT + 1}")
        self.assertIn("ceiling", str(caught.exception))

    def test_a_size_that_is_not_a_number_is_a_typo_and_says_so(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("code:packed=eight")
        self.assertIn("whole number", str(caught.exception))

    def test_a_provider_route_packs_because_an_endpoint_holds_a_wrapper_too(self):
        """What packing needs is a model held to a schema, not a CLI.

        `driver.api_session` answers a packed request from an endpoint on the
        same terms a CLI does — the parent takes the wrapper apart and lands
        each verse against its own packet either way — so a provider route
        packs, and by default, since its input is billed by the token.
        """
        for spec in ("sol:packed=4", "gemini:packed=4", "anthropic:packed=all"):
            route = parse_route(spec)
            self.assertTrue(route.packed, spec)
            self.assertTrue(route.tool_free, spec)
            self.assertFalse(route.is_cli, spec)

    def test_codex_packs_too_and_a_packed_pass_is_tool_free_either_way(self):
        """`codex exec --output-schema` holds the reply to a response schema,
        which is the whole of what packing needs from a CLI."""
        for spec in ("codex:packed=all", "codex/terra:packed=8"):
            route = parse_route(spec)
            self.assertTrue(route.packed, spec)
            self.assertTrue(route.tool_free, spec)
            self.assertTrue(route.is_codex, spec)
        # And an unpacked codex verse is still the agentic session it was.
        self.assertFalse(parse_route("codex").tool_free)
        self.assertFalse(parse_route("codex").packed)

    def test_a_packed_codex_route_round_trips_with_its_model(self):
        self.assertEqual(
            parse_route("codex/terra:packed=8@high").spec(),
            "codex/gpt-5.6-terra:packed=8@high",
        )

    def test_no_other_modifier_takes_a_size(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("sol:batch=4")
        self.assertIn("only :packed takes a size", str(caught.exception))

    def test_a_whole_chapter_pass_cannot_be_packed(self):
        """Neither of them is a verse task, so there is nothing to pack."""
        for mode, spec in (
            ("reconcile", "code:packed=4"), ("decisions", "code:packed=4"),
            ("reconcile", "code:packed=all"),
        ):
            with self.assertRaises(ValueError) as caught:
                resolve_routes(**{mode: spec})
            self.assertIn("cannot be packed", str(caught.exception), mode)


class CodexRouteTests(unittest.TestCase):
    """The other local CLI, which is a peer of `code` and not of `openai`.

    Everything here turns on one distinction: the same model slug reached
    through the Codex CLI and through the API is the same weights and a
    different bill. A route that blurred the two would be a run that spent the
    wrong account and reported nothing unusual.
    """

    def test_a_bare_codex_route_names_the_model_it_will_run(self):
        """Not whatever `~/.codex/config.toml` happens to say that day."""
        route = parse_route("codex")
        self.assertTrue(route.is_codex)
        self.assertTrue(route.is_cli)
        self.assertFalse(route.is_code)
        self.assertEqual(route.name, "gpt-6-astra")

    def test_it_takes_a_model_because_the_cli_does(self):
        self.assertEqual(parse_route("codex/terra").name, "gpt-5.6-terra")
        self.assertEqual(parse_route("codex/terra").spec(), "codex/gpt-5.6-terra")

    def test_a_receipt_can_tell_it_apart_from_the_api(self):
        """`gpt-5.6-sol` alone would name two things that are not the same."""
        self.assertEqual(parse_route("sol").label, "gpt-5.6-sol")
        self.assertEqual(parse_route("codex-sol").label, "gpt-5.6-sol (codex-sol)")
        self.assertEqual(
            parse_route("codex/sol").label, "gpt-5.6-sol (codex)"
        )

    def test_a_bare_model_still_means_the_api(self):
        """The CLI has to be asked for; nothing infers it."""
        self.assertFalse(parse_route("gpt-5.6-sol").is_cli)
        self.assertEqual(parse_route("gpt-5.6-sol").provider, "openai")

    def test_the_model_floor_applies_through_the_cli_too(self):
        """It is a fact about the model, and the CLI does not change it."""
        self.assertEqual(parse_route("codex/terra").resolved_effort("medium"), "high")
        self.assertEqual(parse_route("codex-sol").resolved_effort("medium"), "medium")
        self.assertEqual(parse_route("codex-sol").resolved_effort("high"), "high")

    def test_there_is_no_batch_behind_a_cli(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("codex:batch")
        # And it says where the batch actually is, which is the flag the
        # person typing this wanted.
        self.assertIn("openai:batch", str(caught.exception))

    def test_a_model_from_another_vendor_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            parse_route("codex/claude-opus-5")
        self.assertIn("codex route runs OpenAI models", str(caught.exception))

    def test_every_codex_spelling_round_trips(self):
        for spec in ("codex", "codex@low", "codex/gpt-5.6-terra@high"):
            self.assertEqual(parse_route(spec).spec(), spec)


class CodexAstraTests(unittest.TestCase):
    def test_astra_is_a_subscription_session_with_a_fixed_model(self):
        route = parse_route("codex-astra")
        self.assertTrue(route.is_codex)
        self.assertTrue(route.is_cli)
        self.assertEqual(route.name, "gpt-6-astra")
        self.assertEqual(route.label, "gpt-6-astra (codex-astra)")
        self.assertEqual(session_resume(route, "thread-1"), "codex resume thread-1")
        routes = resolve_routes(**dict.fromkeys(
            ("align", "review", "decisions", "reconcile"), "codex-astra",
        ))
        self.assertEqual(clis_in_use(routes), {"code": False, "codex": True})
        for mode, resolved in routes.items():
            self.assertEqual(resolved.resolved_effort("high"), "medium", mode)
            self.assertEqual(parse_route(resolved.spec()), resolved, mode)

    def test_packing_and_effort_modifiers_round_trip(self):
        for spec in (
            "codex-astra", "codex-astra:packed=all", "codex-astra:packed=8",
            "codex-astra:unpacked", "codex-astra:packed=8@high",
            "codex-astra/gpt-6-astra@medium",
        ):
            self.assertEqual(parse_route(spec).spec(), spec)
        self.assertEqual(resolve_routes(align="codex-astra")["packet"].pack, PACK_ALL)
        self.assertTrue(parse_route("codex-astra:packed=8").tool_free)
        self.assertFalse(parse_route("codex-astra:unpacked").tool_free)
        self.assertEqual(parse_route("codex-astra@high").resolved_effort("medium"), "high")

    def test_invalid_transport_and_model_are_refused(self):
        for spec in ("codex-astra:batch", "codex-astra:session", "codex-astra/sol"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                parse_route(spec)

    def test_a_bare_codex_is_astra_and_the_two_spellings_agree(self):
        """The fallback route reaches the strongest model without being asked.

        `codex` is typed when a Claude pass will not run, so it should be the
        best thing on the other subscription rather than the one that has to be
        remembered. `codex-astra` is the same route said out loud, and the only
        thing that separates them is what a receipt calls it.
        """
        bare, spelled = parse_route("codex"), parse_route("codex-astra")
        self.assertEqual(bare.name, spelled.name)
        self.assertEqual(bare.name, "gpt-6-astra")
        for pass_default in ("medium", "high"):
            self.assertEqual(
                bare.resolved_effort(pass_default),
                spelled.resolved_effort(pass_default),
            )
            self.assertEqual(bare.resolved_effort(pass_default), "medium")
        # Astra's medium is keyed on the model, so it is reached the long way
        # round too — and an explicit effort still wins over it.
        self.assertEqual(
            parse_route("codex/gpt-6-astra").resolved_effort("high"), "medium"
        )
        self.assertEqual(parse_route("codex@high").resolved_effort("medium"), "high")


class CodexSolTests(unittest.TestCase):
    """`codex-sol` is the older default, kept spellable now that it is not one."""

    def test_sol_is_a_subscription_session_with_a_fixed_model(self):
        route = parse_route("codex-sol")
        self.assertTrue(route.is_codex)
        self.assertTrue(route.is_cli)
        self.assertEqual(route.name, "gpt-5.6-sol")
        self.assertEqual(route.label, "gpt-5.6-sol (codex-sol)")
        self.assertEqual(session_resume(route, "thread-1"), "codex resume thread-1")

    def test_it_takes_the_passs_own_effort_rather_than_astras_medium(self):
        """The whole reason to name it: review on Sol is still high."""
        self.assertEqual(parse_route("codex-sol").resolved_effort("medium"), "medium")
        self.assertEqual(parse_route("codex-sol").resolved_effort("high"), "high")

    def test_it_is_the_same_transport_and_packing_as_codex(self):
        routes = resolve_routes(**dict.fromkeys(
            ("align", "review", "decisions", "reconcile"), "codex-sol",
        ))
        self.assertEqual(clis_in_use(routes), {"code": False, "codex": True})
        for mode, resolved in routes.items():
            self.assertEqual(parse_route(resolved.spec()), resolved, mode)
        self.assertEqual(resolve_routes(align="codex-sol")["packet"].pack, PACK_ALL)
        self.assertTrue(parse_route("codex-sol:packed=8").tool_free)
        self.assertFalse(parse_route("codex-sol:unpacked").tool_free)

    def test_every_codex_sol_spelling_round_trips(self):
        for spec in (
            "codex-sol", "codex-sol:packed=all", "codex-sol:packed=8",
            "codex-sol:unpacked", "codex-sol:packed=8@high",
            "codex-sol/gpt-5.6-sol@medium",
        ):
            self.assertEqual(parse_route(spec).spec(), spec)

    def test_invalid_transport_and_model_are_refused(self):
        """A pinned route that honored a model suffix would lie about itself."""
        for spec in ("codex-sol:batch", "codex-sol:session", "codex-sol/terra"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                parse_route(spec)
        with self.assertRaises(ValueError) as caught:
            parse_route("codex-sol/terra")
        # And it says where the model it was asked for actually lives.
        self.assertIn("codex-sol route runs gpt-5.6-sol", str(caught.exception))
        self.assertIn("codex/gpt-5.6-terra", str(caught.exception))

    def test_astra_uses_the_existing_subscription_billing_check(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Halt) as caught:
                ensure_routes_usable(
                    resolve_routes(align="codex-astra"), root=Path(directory),
                    codex_billing=lambda: "OPENAI_API_KEY is set",
                )
        self.assertIn("OPENAI_API_KEY is set", str(caught.exception))


class ChapterPassRouteTests(unittest.TestCase):
    """Which passes a stateless request is the right shape for, and which not.

    A verse is one request and can go anywhere. The other two read a chapter off
    disk over several turns and write back to it, so they route between the
    local CLIs and nowhere else.
    """

    def test_they_default_to_the_same_place_as_everything_else(self):
        routes = resolve_routes()
        for mode in CHAPTER_PASSES:
            self.assertTrue(routes[mode].is_code, mode)
        self.assertTrue(routes["decisions"].session)
        self.assertTrue(routes["reconcile"].tool_free)

    def test_each_may_be_the_other_cli(self):
        routes = resolve_routes(reconcile="codex", decisions="codex")
        for mode in CHAPTER_PASSES:
            self.assertTrue(routes[mode].is_codex, mode)
        # And the verse passes are untouched by it: four separate choices.
        self.assertTrue(routes["packet"].is_code)
        self.assertTrue(routes["review"].is_code)

    def test_one_can_move_without_the_other(self):
        routes = resolve_routes(decisions="codex")
        self.assertTrue(routes["decisions"].is_codex)
        self.assertTrue(routes["reconcile"].is_code)

    def test_an_endpoint_is_refused_rather_than_quietly_downgraded(self):
        for mode in CHAPTER_PASSES:
            for spec in ("sol", "sol:batch", "anthropic", "gemini"):
                with self.assertRaises(ValueError) as caught:
                    resolve_routes(**{mode: spec})
                # Named, and said why, since the reader typed a route that is
                # legal on two of the four passes.
                self.assertIn(mode, str(caught.exception))
                self.assertIn("Use code or codex", str(caught.exception))

    def test_the_verse_passes_may_still_take_one(self):
        """The refusal is about the pass's shape, not about the provider."""
        routes = resolve_routes(align="sol:batch", reconcile="codex", decisions="codex")
        self.assertTrue(routes["packet"].batch)

    def test_a_fully_local_run_is_expressible(self):
        routes = resolve_routes(
            align="codex", review="codex", reconcile="codex", decisions="codex",
        )
        self.assertTrue(all(route.is_codex for route in routes.values()))


class DefaultRouteTests(unittest.TestCase):
    def test_saying_nothing_keeps_the_subsidized_path(self):
        """Claude Code tokens cost a fraction of the API's, so it is the default."""
        routes = resolve_routes()
        self.assertEqual(routes, DEFAULT_ROUTES)
        self.assertTrue(all(route.is_code for route in routes.values()))

    def test_each_pass_is_chosen_separately(self):
        routes = resolve_routes(align="gemini", review="anthropic")
        self.assertEqual(routes["packet"].name, "gemini-3.7-flash")
        self.assertEqual(routes["review"].name, "claude-opus-5")


class EffortFloorTests(unittest.TestCase):
    """How hard the pass asks, and how hard the model needs — two facts."""

    def test_gemini_aligns_at_high_because_medium_is_not_strong_enough(self):
        self.assertEqual(parse_route("gemini").resolved_effort("medium"), "high")

    def test_terra_always_defaults_to_high(self):
        self.assertEqual(parse_route("terra").resolved_effort("medium"), "high")
        self.assertEqual(parse_route("terra").resolved_effort("high"), "high")

    def test_sol_uses_the_pass_default(self):
        self.assertEqual(parse_route("sol").resolved_effort("medium"), "medium")
        self.assertEqual(parse_route("sol").resolved_effort("high"), "high")

    def test_the_models_that_do_not_need_more_take_the_passs_own_default(self):
        for spec in ("openai", "anthropic", "code"):
            self.assertEqual(parse_route(spec).resolved_effort("medium"), "medium")

    def test_a_floor_only_ever_raises(self):
        """Review is high for everything already, so nothing is lowered to it."""
        for spec in ("gemini", "openai", "code"):
            self.assertEqual(parse_route(spec).resolved_effort("high"), "high")

    def test_a_stated_effort_wins_because_a_typed_flag_meant_something(self):
        route = parse_route("gemini@medium")
        self.assertEqual(route.resolved_effort("medium"), "medium")
        # Honoured, but recognisably below what the model is asked for here.
        self.assertTrue(route.understates_the_floor("medium"))

    def test_a_model_with_no_floor_never_understates_one(self):
        self.assertFalse(parse_route("openai@low").understates_the_floor("low"))


class CredentialPreflightTests(unittest.TestCase):
    """A key that is missing is knowable before the chapter is touched."""

    def setUp(self):
        # Whoever runs this may have keys exported; the point here is what
        # happens when they are not, so the environment is emptied and put back.
        self.saved = {
            name: os.environ.pop(name, None) for name, _ in CREDENTIALS.values()
        }

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_a_routed_pass_without_a_key_stops_before_anything_is_spent(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Halt) as caught:
                ensure_routes_usable(
                    resolve_routes(align="gemini"), root=Path(directory),
                )
            halted = caught.exception.as_dict()
            self.assertIn("GEMINI_API_KEY", halted["halted"])
            self.assertIn("packet", halted["halted"])
            self.assertIn("keys.txt", halted["resume"])

    def test_the_code_route_needs_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            ensure_routes_usable(resolve_routes(), root=Path(directory))

    def test_a_key_in_the_file_is_enough_without_an_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keys.txt").write_bytes(b"GEMINI=g-key\r\n")
            ensure_routes_usable(resolve_routes(align="gemini"), root=root)
            # And it is still not in the environment afterwards, because a
            # `claude -p` session started later would inherit it.
            self.assertNotIn("GEMINI_API_KEY", os.environ)


class ResumeTests(unittest.TestCase):
    def test_a_resume_carries_the_routes_it_was_run_with(self):
        """Otherwise half a chapter is aligned by a model nobody chose."""
        command = resume_command(
            "PSA.15", "full", resolve_routes(align="gemini", review="code"),
        )
        self.assertIn("CHAPTER=PSA.15", command)
        self.assertIn("ALIGN=gemini", command)

    def test_a_default_route_is_not_restated(self):
        command = resume_command("PSA.15", "full", resolve_routes())
        self.assertNotIn("ALIGN=", command)
        self.assertNotIn("REVIEW=", command)
        self.assertNotIn("RECONCILE=", command)
        self.assertNotIn("DECISIONS=", command)

    def test_a_resume_carries_the_reconcile_route_too(self):
        """A chapter resumed on the default would reconcile on another model."""
        command = resume_command(
            "PSA.15", "full",
            resolve_routes(align="codex", reconcile="codex", decisions="codex"),
        )
        self.assertIn("ALIGN=codex", command)
        self.assertIn("RECONCILE=codex", command)
        self.assertIn("DECISIONS=codex", command)


class CodexBillingTests(unittest.TestCase):
    """A codex route is a request for the subscription, and only ever that.

    The same models are reachable per token as `sol` and `terra`, so a key
    found anywhere near a codex run is not an ambiguity to resolve in the run's
    favour — it is the wrong account about to be billed under a name that says
    otherwise, which is what GEN.6 cost on the Anthropic side. Both doors are
    checked because either alone lets the other through.
    """

    def setUp(self):
        self.saved = {
            name: os.environ.pop(name, None) for name in CODEX_BILLING_VARS
        }

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    @staticmethod
    def signed_in(said="Logged in using ChatGPT", code=0):
        return lambda args: subprocess.CompletedProcess(args, code, said, "")

    def test_a_key_in_the_environment_is_fatal(self):
        os.environ["OPENAI_API_KEY"] = "sk-should-not-be-spent-here"
        wrong = codex_billing_is_the_subscription(self.signed_in())
        self.assertIn("OPENAI_API_KEY", wrong)

    def test_a_cli_signed_in_with_a_key_is_fatal_too(self):
        """The environment can be clean and the CLI still billing an API key."""
        wrong = codex_billing_is_the_subscription(
            self.signed_in("Logged in using an API key")
        )
        self.assertIn("other than a ChatGPT account", wrong)

    def test_a_cli_that_is_not_signed_in_at_all_stops_the_run(self):
        wrong = codex_billing_is_the_subscription(self.signed_in("", code=1))
        self.assertIn("not signed in", wrong)

    def test_a_missing_cli_is_named_before_a_chapter_is_touched(self):
        def absent(args):
            raise FileNotFoundError(args[0])

        self.assertIn("not on PATH", codex_billing_is_the_subscription(absent))

    def test_the_subscription_passes(self):
        self.assertIsNone(codex_billing_is_the_subscription(self.signed_in()))

    def test_the_preflight_halts_the_run_and_names_the_alternative(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(Halt) as caught:
                ensure_routes_usable(
                    resolve_routes(align="codex"), root=Path(directory),
                    codex_billing=lambda: "OPENAI_API_KEY is set",
                )
        halted = caught.exception.as_dict()
        self.assertIn("packet", halted["halted"])
        self.assertIn("OPENAI_API_KEY", halted["halted"])
        # Not a dead end: the route that does spend a key is named.
        self.assertIn("sol", halted["resume"])

    def test_a_run_with_no_codex_route_never_asks(self):
        """`codex login status` is a subprocess; nothing pays for it idly."""
        def refuse():
            raise AssertionError("billing was checked without a codex route")

        with tempfile.TemporaryDirectory() as directory:
            ensure_routes_usable(
                resolve_routes(), root=Path(directory), codex_billing=refuse,
            )
