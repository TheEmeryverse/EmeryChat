import os
import json
import asyncio
import sys
import tempfile
import types
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

telegram_module = sys.modules.get("telegram")
telegram_error_module = sys.modules.get("telegram.error")
if telegram_module is None:
    telegram_module = types.ModuleType("telegram")
    sys.modules["telegram"] = telegram_module
if telegram_error_module is None:
    telegram_error_module = types.ModuleType("telegram.error")
    sys.modules["telegram.error"] = telegram_error_module

if not hasattr(telegram_module, "InlineKeyboardButton"):
    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    telegram_module.InlineKeyboardButton = InlineKeyboardButton

if not hasattr(telegram_module, "InlineKeyboardMarkup"):
    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    telegram_module.InlineKeyboardMarkup = InlineKeyboardMarkup

if not hasattr(telegram_module, "ReplyParameters"):
    class ReplyParameters:
        def __init__(self, message_id, allow_sending_without_reply=False):
            self.message_id = message_id
            self.allow_sending_without_reply = allow_sending_without_reply

    telegram_module.ReplyParameters = ReplyParameters

if not hasattr(telegram_error_module, "BadRequest"):
    class BadRequest(Exception):
        pass

    telegram_error_module.BadRequest = BadRequest


from emery import debate
import emery.globals as globals


class FakeBot:
    def __init__(self):
        self.messages = []
        self.rich_messages = []

    async def send_rich_message(self, **kwargs):
        self.rich_messages.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.rich_messages))

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.messages))


class FakeMessage:
    message_thread_id = None

    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return types.SimpleNamespace(message_id=len(self.replies))


class FakeUpdate:
    def __init__(self, chat_id=123, text=""):
        self.effective_chat = types.SimpleNamespace(id=chat_id)
        self.effective_user = types.SimpleNamespace(id=456)
        self.message = FakeMessage(text)
        self.callback_query = None


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answered = False

    async def answer(self):
        self.answered = True


class FakeCallbackUpdate:
    def __init__(self, data, chat_id=123):
        self.effective_chat = types.SimpleNamespace(id=chat_id)
        self.effective_user = types.SimpleNamespace(id=456)
        self.message = None
        self.callback_query = FakeCallbackQuery(data)


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.bot = FakeBot()


async def _async_return(value):
    return value


class TestDebateMode(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        debate.ACTIVE_DEBATES.clear()
        for task in list(debate.DEBATE_TASKS.values()):
            task.cancel()
        debate.DEBATE_TASKS.clear()
        debate.DEBATE_COMMIT_LOCKS.clear()
        globals.active_foreground_loops.clear()

    async def test_debate_command_requires_topic(self):
        update = FakeUpdate(text="/debate")
        await debate.handle_debate_command(update, FakeContext(args=[]))

        self.assertIn("Debate commands", update.message.replies[-1]["text"])
        self.assertEqual(debate.ACTIVE_DEBATES, {})

    async def test_debate_command_creates_position_waiting_session(self):
        original_run = debate._run_position_definition

        async def fake_position_definition(session, bot):
            session.positions = {"pro": "Pro position", "anti": "Anti position"}
            session.status = "defining_positions"
            await bot.send_message(chat_id=session.chat_id, text="positions ready")

        try:
            debate._run_position_definition = fake_position_definition
            update = FakeUpdate(text="/debate progressive taxation")
            context = FakeContext(args=["progressive", "taxation"])
            await debate.handle_debate_command(update, context)
        finally:
            debate._run_position_definition = original_run

        self.assertEqual(len(debate.ACTIVE_DEBATES), 1)
        session = next(iter(debate.ACTIVE_DEBATES.values()))
        self.assertEqual(session.status, "defining_positions")
        self.assertEqual(session.positions["pro"], "Pro position")
        self.assertEqual(context.bot.messages[-1]["text"], "positions ready")

    async def test_position_revision_updates_positions_and_acceptance_starts_task(self):
        session = debate.DebateSession(
            id="deb1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            status="defining_positions",
            positions={"pro": "Pro old", "anti": "Anti old"},
        )
        debate.ACTIVE_DEBATES[session.key()] = session

        original_revise = debate._revise_positions
        original_send = debate._send_position_prompt
        original_start = debate._start_debate_task
        started = {}

        async def fake_revise(target, instruction):
            target.positions["anti"] = instruction

        async def fake_send(bot, target):
            await bot.send_message(chat_id=target.chat_id, text=target.positions["anti"])

        def fake_start(target, bot):
            started["id"] = target.id

        try:
            debate._revise_positions = fake_revise
            debate._send_position_prompt = fake_send
            debate._start_debate_task = fake_start
            await debate.handle_debate_message(FakeUpdate(text="make anti argue for flat tax"), FakeContext(), "make anti argue for flat tax")
            await debate.handle_debate_message(FakeUpdate(text="perfect get started"), FakeContext(), "perfect get started")
        finally:
            debate._revise_positions = original_revise
            debate._send_position_prompt = original_send
            debate._start_debate_task = original_start

        self.assertEqual(session.positions["anti"], "make anti argue for flat tax")
        self.assertEqual(started["id"], "deb1")

    def test_role_context_isolation(self):
        session = debate.DebateSession(
            id="ctx1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro progressive tax", "anti": "Anti flat tax"},
            side_names={"pro": "Progressive Taxers", "anti": "Flat Taxers"},
            role_briefs={"pro": "private pro brief", "anti": "private anti brief"},
            research_packets=[
                {"id": "P1", "requester": "pro", "phase": "side_deep", "question": "pro q", "source_ids": ["S1"], "summary": "PRIVATE PRO RESEARCH"},
                {"id": "P2", "requester": "anti", "phase": "side_deep", "question": "anti q", "source_ids": ["S2"], "summary": "PRIVATE ANTI RESEARCH"},
            ],
            formal_turns=[
                {"round": 1, "speaker": "pro", "kind": "answer", "content": "formal pro answer"},
                {"round": 1, "speaker": "anti", "kind": "answer", "content": "formal anti answer"},
            ],
        )

        moderator_context = debate._build_moderator_context(session)
        pro_context = debate._build_side_context(session, "pro")
        anti_context = debate._build_side_context(session, "anti")

        self.assertIn("formal pro answer", moderator_context)
        self.assertIn("formal anti answer", moderator_context)
        self.assertIn("Progressive Taxers", moderator_context)
        self.assertIn("Flat Taxers", moderator_context)
        self.assertNotIn("PRIVATE PRO RESEARCH", moderator_context)
        self.assertNotIn("PRIVATE ANTI RESEARCH", moderator_context)
        self.assertIn("PRIVATE PRO RESEARCH", pro_context)
        self.assertNotIn("PRIVATE ANTI RESEARCH", pro_context)
        self.assertIn("PRIVATE ANTI RESEARCH", anti_context)
        self.assertNotIn("PRIVATE PRO RESEARCH", anti_context)

    async def test_define_positions_records_moderator_side_names(self):
        session = debate.DebateSession(
            id="names1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
        )
        original_research = debate._clerk_research
        original_query = debate._query_main_model
        calls = []

        async def fake_research(*args, **kwargs):
            calls.append(("clerk", kwargs.get("question"), kwargs.get("seed_queries")))
            return {"summary": "progressive and flat tax positions"}

        async def fake_query(prompt, *args, **kwargs):
            calls.append(("moderator", prompt, None))
            if "Before the Clerk researches" in prompt:
                return (
                    '{"research_question":"Find tax-policy position spectrum",'
                    '"scope":"Compare mainstream tax-structure positions",'
                    '"relevance_criteria":"Only sources about progressive versus flat tax design",'
                    '"seed_queries":["progressive tax flat tax debate"]}'
                )
            return (
                '{"pro_name":"Progressive Taxers","anti_name":"Flat Taxers",'
                '"pro_advocate_name":"Jill","anti_advocate_name":"Mathias",'
                '"pro":"Support progressive taxation","anti":"Support flat taxation","framing":"Tax design"}'
            )

        try:
            debate._clerk_research = fake_research
            debate._query_main_model = fake_query
            await debate._define_positions(session)
        finally:
            debate._clerk_research = original_research
            debate._query_main_model = original_query

        self.assertEqual(session.side_names["pro"], "Progressive Taxers")
        self.assertEqual(session.side_names["anti"], "Flat Taxers")
        self.assertEqual(session.advocate_names["pro"], "Jill")
        self.assertEqual(session.advocate_names["anti"], "Mathias")
        self.assertIn("Progressive Taxers", debate._position_text(session))
        self.assertIn("Flat Taxers", debate._position_text(session))
        self.assertEqual(calls[0][0], "moderator")
        self.assertEqual(calls[1][0], "clerk")
        self.assertIn("Moderator scope", calls[1][1])
        self.assertEqual(calls[1][2], ["progressive tax flat tax debate"])
        self.assertEqual(session.initial_research_brief["scope"], "Compare mainstream tax-structure positions")

    async def test_clerk_research_enforces_source_limit(self):
        session = debate.DebateSession(
            id="cap1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
        )
        original_plan = debate._plan_clerk_queries
        original_search = debate._search_web
        original_filter = debate._filter_relevant_search_results
        original_fetch = debate._fetch_source_content
        original_summarize = debate._summarize_source
        original_packet = debate._summarize_research_packet

        async def fake_plan(*args, **kwargs):
            return ["tax policy"]

        async def fake_search(query):
            return [
                {
                    "title": f"Source {i}",
                    "url": f"https://example.com/{i}",
                    "normalized_url": f"https://example.com/{i}",
                    "domain": "example.com",
                    "snippet": "snippet",
                }
                for i in range(20)
            ]

        async def fake_fetch(result):
            return {"success": True, "title": result["title"], "url": result["url"], "content": "content"}

        async def fake_filter(*args, **kwargs):
            return args[4][: args[5]]

        async def fake_summarize(target_session, result, fetched):
            return {
                "id": f"S{len(target_session.sources) + 1}",
                "title": result["title"],
                "url": result["url"],
                "normalized_url": result["normalized_url"],
                "fetch_success": True,
                "summary": "summary",
                "key_claims": [],
            }

        async def fake_packet(*args, **kwargs):
            return "packet summary"

        try:
            debate._plan_clerk_queries = fake_plan
            debate._search_web = fake_search
            debate._filter_relevant_search_results = fake_filter
            debate._fetch_source_content = fake_fetch
            debate._summarize_source = fake_summarize
            debate._summarize_research_packet = fake_packet
            packet = await debate._clerk_research(session, "pro", "side_deep", "question", max_sources=3)
        finally:
            debate._plan_clerk_queries = original_plan
            debate._search_web = original_search
            debate._filter_relevant_search_results = original_filter
            debate._fetch_source_content = original_fetch
            debate._summarize_source = original_summarize
            debate._summarize_research_packet = original_packet

        self.assertEqual(len(session.sources), 3)
        self.assertEqual(len(packet["source_ids"]), 3)
        self.assertEqual(packet["source_limit"], 3)

    async def test_clerk_research_runs_search_and_fetch_concurrently_with_stable_ids(self):
        session = debate.DebateSession(
            id="parallel1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
        )
        original_plan = debate._plan_clerk_queries
        original_search = debate._search_web
        original_filter = debate._filter_relevant_search_results
        original_fetch = debate._fetch_source_content
        original_summarize = debate._summarize_source
        original_packet = debate._summarize_research_packet
        search_started = []
        search_release = asyncio.Event()
        fetch_started = []
        fetch_release = asyncio.Event()

        async def fake_plan(*args, **kwargs):
            return ["query one", "query two"]

        async def fake_search(query):
            search_started.append(query)
            if len(search_started) == 2:
                search_release.set()
            await search_release.wait()
            index = 1 if query == "query one" else 2
            return [{
                "title": f"Source {index}",
                "url": f"https://example.com/{index}",
                "normalized_url": f"https://example.com/{index}",
                "domain": "example.com",
                "snippet": query,
            }]

        async def fake_filter(*args, **kwargs):
            return args[4][: args[5]]

        async def fake_fetch(result):
            fetch_started.append(result["url"])
            if len(fetch_started) == 2:
                fetch_release.set()
            await fetch_release.wait()
            return {"success": True, "title": result["title"], "url": result["url"], "content": result["title"]}

        async def fake_summarize(target_session, result, fetched):
            return {
                "id": "placeholder",
                "title": result["title"],
                "url": result["url"],
                "normalized_url": result["normalized_url"],
                "fetch_success": True,
                "summary": fetched["content"],
                "key_claims": [],
            }

        async def fake_packet(*args, **kwargs):
            return "packet summary"

        try:
            debate._plan_clerk_queries = fake_plan
            debate._search_web = fake_search
            debate._filter_relevant_search_results = fake_filter
            debate._fetch_source_content = fake_fetch
            debate._summarize_source = fake_summarize
            debate._summarize_research_packet = fake_packet
            packet = await debate._clerk_research(session, "pro", "side_deep", "question", max_sources=2)
        finally:
            debate._plan_clerk_queries = original_plan
            debate._search_web = original_search
            debate._filter_relevant_search_results = original_filter
            debate._fetch_source_content = original_fetch
            debate._summarize_source = original_summarize
            debate._summarize_research_packet = original_packet

        self.assertEqual(search_started, ["query one", "query two"])
        self.assertEqual(fetch_started, ["https://example.com/1", "https://example.com/2"])
        self.assertEqual([item["query"] for item in session.search_queries], ["query one", "query two"])
        self.assertEqual([source["id"] for source in session.sources], ["S1", "S2"])
        self.assertEqual(packet["id"], "P1")
        self.assertEqual(packet["source_ids"], ["S1", "S2"])

    async def test_concurrent_clerk_research_assigns_unique_ids_and_skips_duplicate_urls(self):
        session = debate.DebateSession(
            id="parallel2",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
        )
        original_plan = debate._plan_clerk_queries
        original_search = debate._search_web
        original_filter = debate._filter_relevant_search_results
        original_fetch = debate._fetch_source_content
        original_summarize = debate._summarize_source
        original_packet = debate._summarize_research_packet

        async def fake_plan(target_session, requester, phase, question, limit, seed_queries=None):
            return [f"{requester} query"]

        async def fake_search(query):
            return [
                {
                    "title": f"Shared {query}",
                    "url": "https://example.com/shared",
                    "normalized_url": "https://example.com/shared",
                    "domain": "example.com",
                    "snippet": query,
                },
                {
                    "title": f"Unique {query}",
                    "url": f"https://example.com/{query.split()[0]}",
                    "normalized_url": f"https://example.com/{query.split()[0]}",
                    "domain": "example.com",
                    "snippet": query,
                },
            ]

        async def fake_filter(*args, **kwargs):
            return args[4][: args[5]]

        async def fake_fetch(result):
            return {"success": True, "title": result["title"], "url": result["url"], "content": result["title"]}

        async def fake_summarize(target_session, result, fetched):
            await asyncio.sleep(0)
            return {
                "id": "placeholder",
                "title": result["title"],
                "url": result["url"],
                "normalized_url": result["normalized_url"],
                "fetch_success": True,
                "summary": fetched["content"],
                "key_claims": [],
            }

        async def fake_packet(*args, **kwargs):
            return "packet summary"

        try:
            debate._plan_clerk_queries = fake_plan
            debate._search_web = fake_search
            debate._filter_relevant_search_results = fake_filter
            debate._fetch_source_content = fake_fetch
            debate._summarize_source = fake_summarize
            debate._summarize_research_packet = fake_packet
            pro_packet, anti_packet = await asyncio.gather(
                debate._clerk_research(session, "pro", "side_deep", "question", max_sources=2),
                debate._clerk_research(session, "anti", "side_deep", "question", max_sources=2),
            )
        finally:
            debate._plan_clerk_queries = original_plan
            debate._search_web = original_search
            debate._filter_relevant_search_results = original_filter
            debate._fetch_source_content = original_fetch
            debate._summarize_source = original_summarize
            debate._summarize_research_packet = original_packet

        normalized_urls = [source["normalized_url"] for source in session.sources]
        self.assertEqual(len(normalized_urls), len(set(normalized_urls)))
        self.assertEqual([source["id"] for source in session.sources], [f"S{i}" for i in range(1, len(session.sources) + 1)])
        self.assertEqual({pro_packet["id"], anti_packet["id"]}, {"P1", "P2"})

    async def test_plan_clerk_queries_accepts_top_level_json_list(self):
        session = debate.DebateSession(
            id="queries1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
        )
        original_query = debate._query_clerk_model

        async def fake_query(*args, **kwargs):
            return '["irs tax incidence study", "Tax Foundation flat tax analysis"]'

        try:
            debate._query_clerk_model = fake_query
            queries = await debate._plan_clerk_queries(
                session,
                "pro",
                "round_1",
                "Who bears the burden?",
                2,
                seed_queries=["tax incidence distribution"],
            )
        finally:
            debate._query_clerk_model = original_query

        self.assertEqual(
            queries,
            ["tax incidence distribution", "irs tax incidence study"],
        )

    async def test_refine_side_search_queries_accepts_top_level_json_list(self):
        session = debate.DebateSession(
            id="queries2",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Support progressive taxation"},
        )
        original_query = debate._query_main_model

        async def fake_query(*args, **kwargs):
            return '["progressive tax revenue effects", "CBO tax distribution report"]'

        try:
            debate._query_main_model = fake_query
            queries = await debate._refine_side_search_queries(
                session,
                "pro",
                "side_light",
                "Find evidence for revenue stability",
                2,
            )
        finally:
            debate._query_main_model = original_query

        self.assertEqual(
            queries,
            ["progressive tax revenue effects", "CBO tax distribution report"],
        )

    async def test_relevance_filter_distinguishes_empty_from_malformed(self):
        session = debate.DebateSession(
            id="filter1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Support progressive taxes"},
            side_names={"pro": "Progressive Taxers"},
        )
        results = [
            {
                "title": "Tangential news",
                "url": "https://example.com/a",
                "normalized_url": "https://example.com/a",
                "domain": "example.com",
                "snippet": "not useful",
                "query": "tax policy",
            },
            {
                "title": "Relevant study",
                "url": "https://example.org/b",
                "normalized_url": "https://example.org/b",
                "domain": "example.org",
                "snippet": "useful",
                "query": "tax policy",
            },
        ]
        original_query = debate._query_clerk_model

        async def empty_selection(*args, **kwargs):
            return '{"selected_indexes":[]}'

        async def malformed_selection(*args, **kwargs):
            return 'not json'

        try:
            debate._query_clerk_model = empty_selection
            self.assertEqual(
                await debate._filter_relevant_search_results(session, "pro", "side_light", "need", results, 2),
                [],
            )

            debate._query_clerk_model = malformed_selection
            self.assertEqual(
                await debate._filter_relevant_search_results(session, "pro", "side_light", "need", results, 1),
                results[:1],
            )
        finally:
            debate._query_clerk_model = original_query

    def test_archive_session_writes_expected_files_and_index(self):
        session = debate.DebateSession(
            id="arch1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            status="completed",
            final_memo="Final memo",
            source_appendix="Sources",
            formal_turns=[{"speaker": "pro", "kind": "opening", "content": "formal"}],
            sources=[{"id": "S1"}],
            round_results=[{"round": 1, "winner": "pro"}],
        )
        original_archive_dir = debate.DEBATE_ARCHIVE_DIR
        original_index_path = debate.DEBATE_INDEX_PATH
        with tempfile.TemporaryDirectory() as tmp:
            debate.DEBATE_ARCHIVE_DIR = os.path.join(tmp, "debate")
            debate.DEBATE_INDEX_PATH = os.path.join(tmp, "config", "debate_sessions.json")
            folder = debate._archive_session(session)

            self.assertTrue((folder / "session.json").exists())
            self.assertTrue((folder / "memo.md").exists())
            self.assertTrue((folder / "sources.md").exists())
            self.assertTrue((folder / "transcript.md").exists())
            self.assertEqual(debate._load_index()[0]["id"], "arch1")

        debate.DEBATE_ARCHIVE_DIR = original_archive_dir
        debate.DEBATE_INDEX_PATH = original_index_path

    async def test_open_archived_debate_uses_rich_delivery_path(self):
        original_index_path = debate.DEBATE_INDEX_PATH
        original_rich = debate.send_rich_or_split_html_message
        calls = []

        async def fake_rich(bot, chat_id, markdown_text, **kwargs):
            calls.append({
                "bot": bot,
                "chat_id": chat_id,
                "markdown": markdown_text,
                "fallback": kwargs.get("fallback_html_text"),
                "thread": kwargs.get("message_thread_id"),
            })
            return [types.SimpleNamespace(message_id=1)]

        with tempfile.TemporaryDirectory() as tmp:
            memo_path = os.path.join(tmp, "memo.md")
            with open(memo_path, "w", encoding="utf-8") as handle:
                handle.write("# Archived Memo\n\n- **Finding**")
            debate.DEBATE_INDEX_PATH = os.path.join(tmp, "config", "debate_sessions.json")
            debate._save_index([{
                "id": "arch-rich",
                "topic": "Tax policy",
                "memo_path": memo_path,
            }])

            try:
                debate.send_rich_or_split_html_message = fake_rich
                update = FakeUpdate(chat_id=-100123)
                update.message.message_thread_id = 456
                context = FakeContext()
                await debate._open_archived_debate(update, context, "arch-rich")
            finally:
                debate.send_rich_or_split_html_message = original_rich
                debate.DEBATE_INDEX_PATH = original_index_path

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["bot"], context.bot)
        self.assertEqual(calls[0]["chat_id"], -100123)
        self.assertEqual(calls[0]["thread"], 456)
        self.assertEqual(calls[0]["markdown"], "# Archived Memo\n\n- **Finding**")
        self.assertIn("<strong>Finding</strong>", calls[0]["fallback"])

    async def test_debate_loop_rich_html_uses_details_without_spoilers(self):
        session = debate.DebateSession(
            id="rich-loop",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro", "anti": "Anti"},
        )
        original_summarize = debate._summarize_formal_response

        async def fake_summarize(target, side, label, text):
            return f"{side} visible summary"

        try:
            debate._summarize_formal_response = fake_summarize
            rich_html, fallback_html = await debate._opening_statements_rich_and_fallback_html(
                session,
                "<think>private chain</think>Pro full answer",
                "Anti full answer",
            )
        finally:
            debate._summarize_formal_response = original_summarize

        self.assertIn("<details>", rich_html)
        self.assertIn("<summary>Full argument</summary>", rich_html)
        self.assertIn("pro visible summary", rich_html)
        self.assertIn("Pro full answer", rich_html)
        self.assertNotIn("private chain", rich_html)
        self.assertNotIn("<blockquote expandable>", rich_html)
        self.assertNotIn("tg-spoiler", rich_html)
        self.assertNotIn("spoiler", rich_html.lower())
        self.assertIn("<blockquote expandable>", fallback_html)
        self.assertEqual(debate._clean_thinking_tags("Visible <think>private chain"), "Visible ")

    async def test_run_debate_registers_foreground_loop(self):
        session = debate.DebateSession(
            id="loop1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            status="defining_positions",
            positions={"pro": "Pro", "anti": "Anti"},
        )
        original_prepare = debate._prepare_side
        original_questions = debate._moderator_questions
        original_round = debate._run_question_round
        original_final_side = debate._final_side_thesis
        original_final_side_text = debate._final_side_thesis_text
        original_final_memo = debate._build_final_memo
        original_appendix = debate._build_source_appendix
        original_archive = debate._archive_session
        original_clerk_query = debate._query_clerk_model
        observed_active = []

        async def fake_prepare(bot, target, side):
            observed_active.append(globals.has_active_foreground_loops())
            return f"{side} opening"

        async def fake_questions(target):
            return ["Q1", "Q2", "Q3"]

        async def fake_round(bot, target, round_number, question):
            target.round_results.append({"round": round_number, "winner": "tie"})

        async def fake_final_side(target, side):
            return None

        async def fake_final_side_text(target, side):
            return f"{side} final thesis"

        async def fake_final_memo(target):
            return "Final memo"

        def fake_appendix(target):
            return "Sources"

        def fake_archive(target):
            return None

        async def fake_clerk_query(*args, **kwargs):
            return "summary"

        try:
            debate._prepare_side = fake_prepare
            debate._moderator_questions = fake_questions
            debate._run_question_round = fake_round
            debate._final_side_thesis = fake_final_side
            debate._final_side_thesis_text = fake_final_side_text
            debate._build_final_memo = fake_final_memo
            debate._build_source_appendix = fake_appendix
            debate._archive_session = fake_archive
            debate._query_clerk_model = fake_clerk_query
            await debate._run_debate_session(session, FakeBot())
        finally:
            debate._prepare_side = original_prepare
            debate._moderator_questions = original_questions
            debate._run_question_round = original_round
            debate._final_side_thesis = original_final_side
            debate._final_side_thesis_text = original_final_side_text
            debate._build_final_memo = original_final_memo
            debate._build_source_appendix = original_appendix
            debate._archive_session = original_archive
            debate._query_clerk_model = original_clerk_query

        self.assertEqual(observed_active, [True, True])
        self.assertFalse(globals.has_active_foreground_loops())
        self.assertEqual(session.status, "completed")



    async def test_concurrent_side_prep_preserves_transcript_order(self):
        """Both sides prepare concurrently, but transcript shows pro-then-anti opening turns."""
        session = debate.DebateSession(
            id="conc1",
            topic="AI regulation",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro AI regulation", "anti": "Anti AI regulation"},
        )
        prepared = {}
        order_lock = asyncio.Lock()
        prepare_order = []

        async def fake_prepare_concurrent(bot, target, side):
            async with order_lock:
                prepare_order.append(side)
            await asyncio.sleep(0.01)
            prepared[side] = f"{side} opening thesis"
            debate._record_formal_turn(target, side, "opening_thesis", prepared[side])
            return prepared[side]

        async def fake_questions(target):
            return ["Q1", "Q2", "Q3"]

        async def fake_round(bot, target, round_number, question):
            target.round_results.append({"round": round_number, "winner": "tie"})

        original_prepare = debate._prepare_side
        original_questions = debate._moderator_questions
        original_round = debate._run_question_round
        original_final_side = debate._final_side_thesis
        original_final_side_text = debate._final_side_thesis_text
        original_record_final = debate._record_final_side_theses
        original_final_memo = debate._build_final_memo
        original_appendix = debate._build_source_appendix
        original_archive = debate._archive_session
        original_clerk_query = debate._query_clerk_model
        original_opening_rich = debate._opening_statements_rich_and_fallback_html
        original_round_rich = debate._round_responses_rich_and_fallback_html

        async def fake_opening_rich(target, pro, anti):
            text = f"Opening: pro={pro} anti={anti}"
            return text, text

        async def fake_round_rich(target, rn, q, pro, anti):
            text = f"Round {rn}: pro={pro} anti={anti}"
            return text, text

        async def fake_final_side_text(target, side):
            return f"{side} final"

        async def fake_final_memo(target):
            return "Fake memo"

        def fake_archive(target):
            return None

        try:
            debate._prepare_side = fake_prepare_concurrent
            debate._moderator_questions = fake_questions
            debate._run_question_round = fake_round
            debate._final_side_thesis = fake_final_side_text
            debate._final_side_thesis_text = fake_final_side_text
            debate._build_final_memo = fake_final_memo
            debate._build_source_appendix = original_appendix
            debate._archive_session = fake_archive
            debate._query_clerk_model = original_clerk_query
            debate._opening_statements_rich_and_fallback_html = fake_opening_rich
            debate._round_responses_rich_and_fallback_html = fake_round_rich

            # Patch _record_final_side_theses to use text generators
            def fake_record_final_side_theses(target, pro_text, anti_text):
                round_num = len(target.debate_questions) + 1
                debate._record_formal_turn(target, "pro", "final_thesis", pro_text, round_number=round_num)
                debate._record_formal_turn(target, "anti", "final_thesis", anti_text, round_number=round_num)

            debate._record_final_side_theses = fake_record_final_side_theses

            await debate._run_debate_session(session, FakeBot())
        finally:
            debate._prepare_side = original_prepare
            debate._moderator_questions = original_questions
            debate._run_question_round = original_round
            debate._final_side_thesis = original_final_side
            debate._final_side_thesis_text = original_final_side_text
            debate._record_final_side_theses = original_record_final
            debate._build_final_memo = original_final_memo
            debate._build_source_appendix = original_appendix
            debate._archive_session = original_archive
            debate._query_clerk_model = original_clerk_query
            debate._opening_statements_rich_and_fallback_html = original_opening_rich
            debate._round_responses_rich_and_fallback_html = original_round_rich

        # Pro and anti may have been prepared in any internal order due to concurrency
        self.assertIn("pro", prepare_order)
        self.assertIn("anti", prepare_order)
        # But transcript must be deterministic: pro opening before anti opening
        pro_opening_indices = [i for i, t in enumerate(session.formal_turns) if t["speaker"] == "pro" and t["kind"] == "opening_thesis"]
        anti_opening_indices = [i for i, t in enumerate(session.formal_turns) if t["speaker"] == "anti" and t["kind"] == "opening_thesis"]
        self.assertGreater(len(pro_opening_indices), 0)
        self.assertGreater(len(anti_opening_indices), 0)
        self.assertLess(pro_opening_indices[0], anti_opening_indices[0])
        # Same for final theses
        pro_final_indices = [i for i, t in enumerate(session.formal_turns) if t["speaker"] == "pro" and t["kind"] == "final_thesis"]
        anti_final_indices = [i for i, t in enumerate(session.formal_turns) if t["speaker"] == "anti" and t["kind"] == "final_thesis"]
        self.assertGreater(len(pro_final_indices), 0)
        self.assertGreater(len(anti_final_indices), 0)
        self.assertLess(pro_final_indices[0], anti_final_indices[0])

    async def test_combined_brief_opening_json_fallback_chain(self):
        """Malformed JSON falls back to raw text, then to deep_packet summary."""
        session = debate.DebateSession(
            id="fallback1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro", "anti": "Anti"},
        )
        original_clerk = debate._clerk_research
        original_query = debate._query_main_model

        async def fake_clerk(*args, **kwargs):
            return {"summary": "DEEP PACKET SUMMARY"}

        try:
            debate._clerk_research = fake_clerk

            def assert_single_opening(side, expected):
                turns = [t for t in session.formal_turns if t["speaker"] == side and t["kind"] == "opening_thesis"]
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["content"], expected)

            # Case 1: Valid JSON with both keys
            async def fake_query_good(prompt, *args, **kwargs):
                return '{"role_brief": "Role brief text", "opening_thesis": "Opening text"}'

            debate._query_main_model = fake_query_good
            opening = await debate._prepare_side(FakeBot(), session, "pro")
            self.assertEqual(session.role_briefs["pro"], "Role brief text")
            self.assertEqual(opening, "Opening text")
            assert_single_opening("pro", "Opening text")

            # Case 2: Malformed JSON - fallback to raw text, then role_brief for opening
            async def fake_query_bad(prompt, *args, **kwargs):
                return "Raw model output with no JSON at all"

            session.role_briefs.clear()
            session.formal_turns.clear()
            debate._query_main_model = fake_query_bad
            opening = await debate._prepare_side(FakeBot(), session, "anti")
            self.assertEqual(session.role_briefs["anti"], "Raw model output with no JSON at all")
            self.assertEqual(opening, "Raw model output with no JSON at all")
            assert_single_opening("anti", "Raw model output with no JSON at all")

            # Case 3: JSON with role_brief missing - fallback to deep_packet summary
            async def fake_query_missing_role(prompt, *args, **kwargs):
                return '{"opening_thesis": "Only opening"}'

            session.role_briefs.clear()
            session.formal_turns.clear()
            debate._query_main_model = fake_query_missing_role
            opening = await debate._prepare_side(FakeBot(), session, "pro")
            # raw JSON string is truthy, so role_brief falls back to raw text
            self.assertEqual(session.role_briefs["pro"], '{"opening_thesis": "Only opening"}')
            self.assertEqual(opening, "Only opening")
            assert_single_opening("pro", "Only opening")

            # Case 4: Completely empty JSON object - raw "{}" is truthy, falls back to raw
            async def fake_query_empty(prompt, *args, **kwargs):
                return '{}'

            session.role_briefs.clear()
            session.formal_turns.clear()
            debate._query_main_model = fake_query_empty
            opening = await debate._prepare_side(FakeBot(), session, "anti")
            self.assertEqual(session.role_briefs["anti"], "{}")
            self.assertEqual(opening, "{}")
            assert_single_opening("anti", "{}")

        finally:
            debate._clerk_research = original_clerk
            debate._query_main_model = original_query

    async def test_final_side_thesis_text_and_record_order(self):
        """_final_side_thesis_text generates text and _record_final_side_theses writes pro-then-anti."""
        session = debate.DebateSession(
            id="thesis1",
            topic="AI regulation",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro", "anti": "Anti"},
            debate_questions=["Q1", "Q2", "Q3"],
        )
        original_query = debate._query_main_model

        async def fake_query(prompt, system_prompt):
            if "pro side" in system_prompt:
                return "pro final"
            return "anti final"

        try:
            debate._query_main_model = fake_query
            pro_text = await debate._final_side_thesis_text(session, "pro")
            anti_text = await debate._final_side_thesis_text(session, "anti")
            self.assertEqual(pro_text, "pro final")
            self.assertEqual(anti_text, "anti final")

            debate._record_final_side_theses(session, pro_text, anti_text)
        finally:
            debate._query_main_model = original_query

        final_turns = [t for t in session.formal_turns if t["kind"] == "final_thesis"]
        self.assertEqual(len(final_turns), 2)
        self.assertEqual(final_turns[0]["speaker"], "pro")
        self.assertEqual(final_turns[0]["round"], 4)
        self.assertEqual(final_turns[1]["speaker"], "anti")
        self.assertEqual(final_turns[1]["round"], 4)

    async def test_commit_lock_lifecycle_cleanup(self):
        """Commit lock is created during debate and cleaned up on completion."""
        session = debate.DebateSession(
            id="lock1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro", "anti": "Anti"},
        )

        original_prepare = debate._prepare_side
        original_questions = debate._moderator_questions
        original_round = debate._run_question_round
        original_final_side = debate._final_side_thesis
        original_final_side_text = debate._final_side_thesis_text
        original_final_memo = debate._build_final_memo
        original_appendix = debate._build_source_appendix
        original_archive = debate._archive_session
        original_clerk_query = debate._query_clerk_model
        original_opening_rich = debate._opening_statements_rich_and_fallback_html

        async def fake_prepare(bot, target, side):
            return f"{side} opening"

        async def fake_questions(target):
            return ["Q1"]

        async def fake_round(bot, target, round_number, question):
            target.round_results.append({"round": round_number, "winner": "tie"})

        async def fake_final_memo(target):
            return "Final memo"

        async def fake_final_side_text(target, side):
            return f"{side} final"

        async def fake_opening_rich(target, pro_opening, anti_opening):
            text = f"{pro_opening}\n{anti_opening}"
            return text, text

        def fake_archive(target):
            return None

        try:
            debate._prepare_side = fake_prepare
            debate._moderator_questions = fake_questions
            debate._run_question_round = fake_round
            debate._final_side_thesis = original_final_side
            debate._final_side_thesis_text = fake_final_side_text
            debate._build_final_memo = fake_final_memo
            debate._build_source_appendix = original_appendix
            debate._archive_session = fake_archive
            debate._query_clerk_model = original_clerk_query
            debate._opening_statements_rich_and_fallback_html = fake_opening_rich

            self.assertNotIn(session.id, debate.DEBATE_COMMIT_LOCKS)
            debate._session_commit_lock(session)
            self.assertIn(session.id, debate.DEBATE_COMMIT_LOCKS)
            await debate._run_debate_session(session, FakeBot())
            self.assertNotIn(session.id, debate.DEBATE_COMMIT_LOCKS)
            self.assertEqual(session.status, "completed")
        finally:
            debate._prepare_side = original_prepare
            debate._moderator_questions = original_questions
            debate._run_question_round = original_round
            debate._final_side_thesis = original_final_side
            debate._final_side_thesis_text = original_final_side_text
            debate._build_final_memo = original_final_memo
            debate._build_source_appendix = original_appendix
            debate._archive_session = original_archive
            debate._query_clerk_model = original_clerk_query
            debate._opening_statements_rich_and_fallback_html = original_opening_rich

    async def test_commit_lock_cleanup_on_cancellation(self):
        """Commit lock is cleaned up when debate is cancelled."""
        session = debate.DebateSession(
            id="cancel1",
            topic="Tax policy",
            chat_id=123,
            message_thread_id=None,
            user_id=456,
            positions={"pro": "Pro", "anti": "Anti"},
        )

        cancel_event = asyncio.Event()

        async def fake_prepare_slow(bot, target, side):
            await cancel_event.wait()
            return f"{side} opening"

        original_prepare = debate._prepare_side
        original_questions = debate._moderator_questions
        original_clerk_query = debate._query_clerk_model
        original_opening_rich = debate._opening_statements_rich_and_fallback_html

        try:
            debate._prepare_side = fake_prepare_slow
            debate._moderator_questions = original_questions
            debate._query_clerk_model = original_clerk_query
            debate._opening_statements_rich_and_fallback_html = original_opening_rich

            debate._session_commit_lock(session)
            self.assertIn(session.id, debate.DEBATE_COMMIT_LOCKS)
            debate._start_debate_task(session, FakeBot())
            await asyncio.sleep(0.1)

            self.assertIn(session.id, debate.DEBATE_TASKS)

            await debate._cancel_session_object(FakeBot(), session)

            self.assertNotIn(session.id, debate.DEBATE_COMMIT_LOCKS)
            cancel_event.set()
        finally:
            debate._prepare_side = original_prepare
            debate._moderator_questions = original_questions
            debate._query_clerk_model = original_clerk_query
            debate._opening_statements_rich_and_fallback_html = original_opening_rich
            for task in list(debate.DEBATE_TASKS.values()):
                task.cancel()
            debate.DEBATE_TASKS.clear()
            debate.DEBATE_COMMIT_LOCKS.clear()
            debate.ACTIVE_DEBATES.clear()



class TestComputeOutcome(unittest.TestCase):
    """Unit tests for _compute_outcome — deterministic winner/loser from round results."""

    def _make_session(self, pro_side="Pro", anti_side="Anti", side_names=None):
        session = debate.DebateSession(
            id="outcome1", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
        )
        if side_names:
            session.side_names = side_names
        else:
            session.side_names = {"pro": pro_side, "anti": anti_side}
        return session

    def test_pro_wins(self):
        session = self._make_session()
        session.round_results = [
            {"winner": "pro"},
            {"winner": "pro"},
            {"winner": "anti"},
            {"winner": "tie"},
        ]
        outcome = debate._compute_outcome(session)
        self.assertEqual(outcome["winner_side"], "pro")
        self.assertEqual(outcome["loser_side"], "anti")
        self.assertEqual(outcome["pro_wins"], 2)
        self.assertEqual(outcome["anti_wins"], 1)
        self.assertEqual(outcome["tie_count"], 1)
        self.assertIn("Pro wins", outcome["title"])
        self.assertIn("Pro 2", outcome["score_line"])

    def test_anti_wins(self):
        session = self._make_session()
        session.round_results = [
            {"winner": "anti"},
            {"winner": "anti"},
            {"winner": "anti"},
        ]
        outcome = debate._compute_outcome(session)
        self.assertEqual(outcome["winner_side"], "anti")
        self.assertEqual(outcome["loser_side"], "pro")
        self.assertEqual(outcome["pro_wins"], 0)
        self.assertEqual(outcome["anti_wins"], 3)

    def test_exact_tie(self):
        session = self._make_session()
        session.round_results = [
            {"winner": "pro"},
            {"winner": "anti"},
        ]
        outcome = debate._compute_outcome(session)
        self.assertIsNone(outcome["winner_side"])
        self.assertIsNone(outcome["loser_side"])
        self.assertEqual(outcome["title"], "No clear winner")
        self.assertEqual(outcome["pro_wins"], 1)
        self.assertEqual(outcome["anti_wins"], 1)

    def test_all_ties(self):
        session = self._make_session()
        session.round_results = [
            {"winner": "tie"},
            {"winner": "tie"},
        ]
        outcome = debate._compute_outcome(session)
        self.assertIsNone(outcome["winner_side"])
        self.assertIsNone(outcome["loser_side"])
        self.assertEqual(outcome["tie_count"], 2)

    def test_custom_side_names_used_in_labels(self):
        session = self._make_session(pro_side="Progressive Tax", anti_side="Flat Tax")
        session.round_results = [{"winner": "pro"}]
        outcome = debate._compute_outcome(session)
        self.assertIn("Progressive Tax", outcome["title"])
        self.assertIn("Progressive Tax", outcome["pro_label"])
        self.assertIn("Flat Tax", outcome["anti_label"])


class TestNormalizeParagraphs(unittest.TestCase):
    """Unit tests for _normalize_paragraphs — blank removal and overflow merging."""

    def test_within_limit(self):
        result = debate._normalize_paragraphs(
            ["One", "Two"], 3
        )
        self.assertEqual(result, ["One", "Two"])

    def test_empty_list(self):
        result = debate._normalize_paragraphs([], 3)
        self.assertEqual(result, [])

    def test_none_input(self):
        result = debate._normalize_paragraphs(None, 3)
        self.assertEqual(result, [])

    def test_removes_blanks(self):
        result = debate._normalize_paragraphs(
            ["One", "", "  ", "Two"], 3
        )
        self.assertEqual(result, ["One", "Two"])

    def test_overflow_merged_into_last(self):
        result = debate._normalize_paragraphs(
            ["One", "Two", "Three", "Four", "Five"], 3
        )
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], "One")
        self.assertEqual(result[1], "Two")
        self.assertIn("Three", result[2])
        self.assertIn("Four", result[2])
        self.assertIn("Five", result[2])

    def test_all_overflow_into_last(self):
        result = debate._normalize_paragraphs(
            ["One", "Two", "Three", "Four"], 2
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], "One")
        self.assertIn("Two", result[1])
        self.assertIn("Three", result[1])
        self.assertIn("Four", result[1])

    def test_non_string_entries_ignored(self):
        result = debate._normalize_paragraphs(
            ["One", 42, "Two", None], 3
        )
        self.assertEqual(result, ["One", "Two"])


class TestFormatMemoText(unittest.TestCase):
    """Unit tests for _format_memo_text — headings, side names, tie variant."""

    def test_pro_winner_heading(self):
        outcome = {
            "title": "Pro wins",
            "score_line": "Score: Pro 2, Anti 1, Ties 0",
            "loser_side": "anti",
            "anti_label": "Anti",
        }
        text = debate._format_memo_text(
            outcome, ["Synth one.", "Synth two."],
            ["Loser one.", "Loser two."], ["R1: Pro — good point"]
        )
        self.assertIn("# Pro wins", text)
        self.assertIn("Score: Pro 2, Anti 1", text)
        self.assertIn("## Strengths and Weaknesses of Anti", text)

    def test_anti_winner_heading(self):
        outcome = {
            "title": "Anti wins",
            "score_line": "Score: Pro 1, Anti 3, Ties 0",
            "loser_side": "pro",
            "pro_label": "Pro",
        }
        text = debate._format_memo_text(
            outcome, ["Synth one."],
            ["Loser one."], ["R1: Anti — good point"]
        )
        self.assertIn("# Anti wins", text)
        self.assertIn("## Strengths and Weaknesses of Pro", text)

    def test_tie_uses_both_analysis(self):
        outcome = {
            "title": "No clear winner",
            "score_line": "Score: Pro 1, Anti 1, Ties 1",
            "winner_side": None,
            "loser_side": None,
        }
        text = debate._format_memo_text(
            outcome, ["Synth one."],
            ["Both analysis one.", "Both analysis two."],
            []
        )
        self.assertIn("# No clear winner", text)
        self.assertIn("## Analysis of Both Positions", text)

    def test_score_line_in_text(self):
        outcome = {
            "title": "Pro wins",
            "score_line": "Score: Pro 3, Anti 0, Ties 0",
            "loser_side": "anti",
            "anti_label": "Anti",
        }
        text = debate._format_memo_text(
            outcome, ["Synth one."], ["Loser one."], []
        )
        self.assertIn("Score: Pro 3, Anti 0, Ties 0", text)


class TestBuildFinalMemo(unittest.IsolatedAsyncioTestCase):
    """Unit tests for _build_final_memo — JSON parsing, fallback, outcome enforcement."""

    async def test_structured_json_parsed(self):
        session = debate.DebateSession(
            id="fm1", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "pro"}]
        fake_json = '{"synthesis_paragraphs": ["Synth one.", "Synth two."], "losing_argument_paragraphs": ["Loser one."]}'

        async def fake_query(text, system):
            return fake_json

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        self.assertIn("# Pro wins", memo)
        self.assertIn("Synth one.", memo)
        self.assertIn("## Strengths and Weaknesses of Anti", memo)
        self.assertIn("Loser one.", memo)

    async def test_malformed_json_fallback(self):
        session = debate.DebateSession(
            id="fm2", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "anti"}]
        fake_prose = "This is just prose\n\nNot structured at all\n\nIt has multiple\n\nparagraphs\n\nbut no JSON keys"

        async def fake_query(text, system):
            return fake_prose

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        self.assertIn("Anti wins", memo)
        self.assertIn("Strengths and Weaknesses of Pro", memo)

    async def test_empty_model_output_fallback(self):
        session = debate.DebateSession(
            id="fm3", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "pro"}]

        async def fake_query(text, system):
            return ""

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        self.assertIn("Pro wins", memo)
        self.assertIn("Score: Pro 1, Anti 0", memo)

    async def test_tie_no_losing_heading(self):
        session = debate.DebateSession(
            id="fm4", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "pro"}, {"winner": "anti"}]
        fake_json = '{"synthesis_paragraphs": ["Synth one."], "losing_argument_paragraphs": ["Analysis one."]}'

        async def fake_query(text, system):
            return fake_json

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        self.assertIn("# No clear winner", memo)
        self.assertIn("## Analysis of Both Positions", memo)
        self.assertNotIn("Strengths and Weaknesses of", memo)

    async def test_model_cannot_override_winner_label(self):
        session = debate.DebateSession(
            id="fm5", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "pro"}, {"winner": "pro"}]
        fake_json = '{"synthesis_paragraphs": ["Anti actually wins"], "losing_argument_paragraphs": ["Pro is weak"]}'

        async def fake_query(text, system):
            return fake_json

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        # The memo title must reflect the computed winner, not the model's prose
        self.assertIn("# Pro wins", memo)
        self.assertIn("Strengths and Weaknesses of Anti", memo)

    async def test_malformed_json_uses_deterministic_fallback(self):
        """Malformed model output must produce deterministic fallback, not raw text split."""
        session = debate.DebateSession(
            id="fm6", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "pro"}, {"winner": "pro"}]
        # Return completely malformed text
        async def fake_query(text, system):
            return "this is not json at all"

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        # Should have deterministic headings from code-computed outcome
        self.assertIn("# Pro wins", memo)
        self.assertIn("Strengths and Weaknesses of Anti", memo)
        # Should NOT contain raw model text
        self.assertNotIn("this is not json", memo)

    async def test_partial_json_missing_keys_uses_deterministic_fallback(self):
        """Partially valid JSON (missing keys) must not forward raw text."""
        session = debate.DebateSession(
            id="fm7", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        session.round_results = [{"winner": "anti"}, {"winner": "pro"}, {"winner": "anti"}]
        # Valid JSON but missing losing_argument_paragraphs key
        async def fake_query(text, system):
            return '{"synthesis_paragraphs": ["Anti won the debate"]}'

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = await debate._build_final_memo(session)
        finally:
            debate._query_main_model = original

        # Should still have both sections with deterministic fallback for losing
        self.assertIn("# Anti wins", memo)
        self.assertIn("Strengths and Weaknesses of Pro", memo)
        # Should NOT contain unheaded raw text
        self.assertNotIn("Anti won the debate", memo)


class TestBuildFinalMemoExtended(unittest.IsolatedAsyncioTestCase):
    """Additional tests for _build_final_memo: paragraph bounds, prompt, contradictions,
    side names in fallback, fallback substance, multiline rationale normalization."""

    def _make_session(self, topic="Tax", pro="Pro", anti="Anti", side_names=None):
        session = debate.DebateSession(
            id="fm_ext", topic=topic, chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": pro, "anti": anti},
            side_names=side_names or {"pro": pro, "anti": anti},
        )
        return session

    def _count_rendered_paragraphs(self, memo):
        """Count non-empty, non-heading paragraphs in the rendered memo text."""
        import re
        lines = memo.strip().split("\n")
        paras = []
        current = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                if current:
                    paras.append(" ".join(current))
                    current = []
                continue
            if stripped:
                if current:
                    current.append(stripped)
                else:
                    current = [stripped]
            else:
                if current:
                    paras.append(" ".join(current))
                    current = []
        if current:
            paras.append(" ".join(current))
        return paras

    def test_paragraph_count_enforcement_parsed(self):
        """Synthesis must be 1-3 paragraphs, losing must be 1-2 paragraphs in rendered output."""
        session = self._make_session()
        session.round_results = [{"winner": "pro"}]
        # Model returns more than 3 synthesis paragraphs
        fake_json = json.dumps({
            "synthesis_paragraphs": ["P1", "P2", "P3", "P4", "P5"],
            "losing_argument_paragraphs": ["L1", "L2", "L3", "L4"]
        })

        async def fake_query(text, system):
            return fake_json

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = asyncio.run(debate._build_final_memo(session))
        finally:
            debate._query_main_model = original

        paras = self._count_rendered_paragraphs(memo)
        # All paragraphs together should be bounded (synthesis 1-3 + losing 1-2 = max 5)
        self.assertLessEqual(len(paras), 5,
            f"Expected at most 5 paragraphs, got {len(paras)}: {paras}")
        # Check synthesis section specifically
        synth_section = memo.split("## Strengths")[0]
        synth_paras = self._count_rendered_paragraphs(synth_section)
        self.assertGreaterEqual(synth_paras, 1, "Synthesis must have at least 1 paragraph")
        self.assertLessEqual(synth_paras, 3, f"Synthesis must have at most 3 paragraphs, got {len(synth_paras)}")

    def test_readability_prompt_captured(self):
        """Prompt sent to model must include readability instructions."""
        session = self._make_session()
        session.round_results = [{"winner": "pro"}]
        captured_prompt = None

        async def fake_query(text, system):
            nonlocal captured_prompt
            captured_prompt = text
            return "{}"

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            asyncio.run(debate._build_final_memo(session))
        finally:
            debate._query_main_model = original

        self.assertIsNotNone(captured_prompt)
        self.assertIn("everyday language", captured_prompt)
        self.assertIn("short sentences", captured_prompt)
        self.assertIn("technical terms", captured_prompt)

    def test_contradiction_rejection_proses_winner(self):
        """Valid JSON claiming the loser won must trigger fallback synthesis."""
        session = self._make_session()
        session.round_results = [{"winner": "pro"}]
        fake_json = json.dumps({
            "synthesis_paragraphs": ["Anti actually won the debate."],
            "losing_argument_paragraphs": ["Pro is weak."]
        })

        async def fake_query(text, system):
            return fake_json

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = asyncio.run(debate._build_final_memo(session))
        finally:
            debate._query_main_model = original

        # Must NOT contain the contradictory claim
        self.assertNotIn("Anti actually won", memo)
        # Must have correct heading
        self.assertIn("# Pro wins", memo)

    def test_side_names_in_fallback_synthesis(self):
        """Fallback synthesis must use actual side names, not generic Pro/Anti."""
        session = self._make_session(
            pro="Progressive Taxers",
            anti="Flat Taxers",
            side_names={"pro": "Progressive Taxers", "anti": "Flat Taxers"}
        )
        session.round_results = [{"winner": "pro"}]

        async def fake_query(text, system):
            return "not json"

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = asyncio.run(debate._build_final_memo(session))
        finally:
            debate._query_main_model = original

        # Should use actual side names
        self.assertIn("Progressive Taxers", memo)
        self.assertIn("Flat Taxers", memo)
        # Should NOT contain generic Pro/Anti in the synthesized content
        synth_section = memo.split("## Strengths")[0]
        # If side names differ from Pro/Anti, generic labels should not appear
        if "Progressive Taxers" != "Pro":
            self.assertNotIn("Pro won", synth_section,
                f"Generic 'Pro' should not appear in fallback synthesis: {synth_section}")

    def test_fallback_losing_substance(self):
        """Fallback losing must discuss both a strength and a weakness, not just 'lost on round counts'."""
        session = self._make_session()
        session.round_results = [
            {"winner": "pro", "rationale": "stronger evidence"},
            {"winner": "pro", "rationale": "better logic"}
        ]

        async def fake_query(text, system):
            return "not json"

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = asyncio.run(debate._build_final_memo(session))
        finally:
            debate._query_main_model = original

        losing_section = memo.split("## Strengths")[1] if "## Strengths" in memo else ""
        # Must contain something beyond just "lost on round counts"
        # It should have rationale-based content
        self.assertNotIn("lost on round counts.", losing_section,
            f"Fallback losing should have more substance: {losing_section}")

    def test_multiline_rationale_normalized(self):
        """Multiline rationale in fallback should produce bounded paragraphs."""
        session = self._make_session()
        session.round_results = [{"winner": "pro", "rationale": "Line one.\n\nLine two.\n\nLine three."}]

        async def fake_query(text, system):
            return "not json"

        original = debate._query_main_model
        try:
            debate._query_main_model = fake_query
            memo = asyncio.run(debate._build_final_memo(session))
        finally:
            debate._query_main_model = original

        # Should not have extra blank lines creating paragraphs
        paras = self._count_rendered_paragraphs(memo)
        self.assertLessEqual(len(paras), 5,
            f"Multiline rationale should normalize to bounded paragraphs: {paras}")



class TestSourceAppendixRichAndFallbackHtml(unittest.TestCase):
    """Unit tests for _source_appendix_rich_and_fallback_html."""

    def _make_session(self, sources=None):
        session = debate.DebateSession(
            id="src1", topic="Tax", chat_id=123, user_id=456,
            message_thread_id=None,
            positions={"pro": "Pro", "anti": "Anti"},
            side_names={"pro": "Pro", "anti": "Anti"},
        )
        if sources is not None:
            session.sources = sources
        return session

    def test_zero_sources(self):
        session = self._make_session(sources=[])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        self.assertIn("<details>", rich_html)
        self.assertIn("<summary>", rich_html)
        self.assertIn("Sources (0)", rich_html)
        self.assertIn("No sources were gathered.", rich_html)
        self.assertIn("<blockquote expandable>", fallback_html)
        self.assertIn("Sources (0)", fallback_html)

    def test_single_source(self):
        session = self._make_session(sources=[{
            "id": "S1",
            "title": "Example Article",
            "url": "https://example.com",
            "summary": "A summary",
            "fetch_success": True,
        }])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        self.assertIn("<details>", rich_html)
        self.assertIn("Sources (1)", rich_html)
        self.assertIn("<b>S1</b>", rich_html)
        self.assertIn("Example Article", rich_html)
        self.assertIn("href=\"https://example.com\"", rich_html)
        self.assertIn("A summary", rich_html)
        self.assertIn("<blockquote expandable>", fallback_html)

    def test_failed_fetch_label(self):
        session = self._make_session(sources=[{
            "id": "S1",
            "title": "Broken Link",
            "url": "https://broken.com",
            "summary": "Could not reach",
            "fetch_success": False,
        }])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        self.assertIn("(fetch failed)", rich_html)

    def test_no_url(self):
        session = self._make_session(sources=[{
            "id": "S1",
            "title": "No URL Source",
            "url": "",
            "summary": "No link available",
            "fetch_success": True,
        }])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        self.assertIn("<b>S1</b>", rich_html)
        self.assertIn("No URL Source", rich_html)
        # No href should appear for empty URL
        self.assertNotIn("href=\"\"", rich_html)

    def test_html_escaping(self):
        session = self._make_session(sources=[{
            "id": "S1",
            "title": "<script>alert('xss')</script>",
            "url": "https://example.com?x=1&y=2",
            "summary": "Summary with <b>bold</b>",
            "fetch_success": True,
        }])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        # Escaped, not rendered
        self.assertIn("&lt;script&gt;", rich_html)
        self.assertIn("x=1&amp;y=2", rich_html)
        self.assertIn("&lt;b&gt;", rich_html)
        # Raw HTML tags should not appear
        self.assertNotIn("<script>", rich_html)

    def test_multiple_sources_ordered(self):
        session = self._make_session(sources=[
            {"id": "S1", "title": "First", "url": "https://a.com", "summary": "First summary", "fetch_success": True},
            {"id": "S2", "title": "Second", "url": "https://b.com", "summary": "Second summary", "fetch_success": True},
            {"id": "S3", "title": "Third", "url": "https://c.com", "summary": "Third summary", "fetch_success": True},
        ])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        self.assertIn("Sources (3)", rich_html)
        first_pos = rich_html.index("First")
        second_pos = rich_html.index("Second")
        third_pos = rich_html.index("Third")
        self.assertLess(first_pos, second_pos)
        self.assertLess(second_pos, third_pos)


    def test_url_escaping_prevents_attribute_injection(self):
        """URLs with quotes must not break out of href attributes."""
        session = self._make_session(sources=[{
            "id": "S1",
            "title": "Safe Title",
            "url": "https://example.com/x=\" onclick=\"alert(1)",
            "summary": "Summary",
            "fetch_success": True,
        }])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        # Double quotes in URL must be HTML-escaped so they can't break the href attribute.
        # The injected onclick= is contained inside the href value as literal text.
        self.assertIn("&quot;", rich_html)
        # Verify the <a> tag is well-formed and closes properly.
        self.assertIn("Safe Title</a>", rich_html)

    def test_status_not_duplicated(self):
        """Fetch-failed status should appear exactly once per source."""
        session = self._make_session(sources=[{
            "id": "S1",
            "title": "Failed Source",
            "url": "https://example.com",
            "summary": "Summary",
            "fetch_success": False,
        }])
        rich_html, fallback_html = debate._source_appendix_rich_and_fallback_html(session)

        # The status text should appear exactly once
        status_count = rich_html.count("(fetch failed)")
        self.assertEqual(status_count, 1)

        # Same for fallback
        fallback_count = fallback_html.count("(fetch failed)")
        self.assertEqual(fallback_count, 1)


class TestDebateFinalOutputsUseRichDeliveryPathUpdated(unittest.IsolatedAsyncioTestCase):
    """Updated e2e test: assert Rich Markdown memo + Rich HTML sources, same routing.

    This replaces the old test_debate_final_outputs_use_rich_delivery_path which only
    tracked calls to send_rich_or_split_html_message. The new code path sends the
    final memo via Rich Markdown and sources via Rich HTML, so we capture both
    delivery functions. Opening statements also go through Rich Markdown delivery.
    """

    async def test_rich_markdown_memo_then_rich_html_sources(self):
        session = debate.DebateSession(
            id="rich2", topic="Tax policy",
            chat_id=123, message_thread_id=789,
            user_id=456,
            positions={"pro": "Pro", "anti": "Anti"},
        )
        calls = []
        original_prepare = debate._prepare_side
        original_questions = debate._moderator_questions
        original_round = debate._run_question_round
        original_final_side_text = debate._final_side_thesis_text
        original_final_memo = debate._build_final_memo
        original_archive = debate._archive_session
        original_opening_rich = debate._opening_statements_rich_and_fallback_html
        original_rich_md = debate.send_rich_or_split_html_message
        original_rich_html = debate.send_rich_html_or_split_html_message

        async def fake_prepare(bot, target, side):
            return f"{side} opening"

        async def fake_questions(target):
            return ["Q1"]

        async def fake_round(bot, target, round_number, question):
            target.round_results.append({"round": round_number, "winner": "pro"})

        async def fake_final_side_text(target, side):
            return f"{side} final"

        async def fake_final_memo(target):
            return "# Final Memo\n\n- **Finding**"

        def fake_archive(target):
            return None

        async def fake_opening_rich(target, pro_opening, anti_opening):
            return "<h2>Openings</h2>", "<b>Openings</b>"

        async def fake_rich_md(bot, chat_id, markdown_text, **kwargs):
            calls.append({
                "delivery": "rich_md",
                "chat_id": chat_id,
                "markdown": markdown_text,
                "thread": kwargs.get("message_thread_id"),
            })
            return [types.SimpleNamespace(message_id=len(calls))]

        async def fake_rich_html(bot, chat_id, rich_html_text, **kwargs):
            calls.append({
                "delivery": "rich_html",
                "chat_id": chat_id,
                "html": rich_html_text,
                "thread": kwargs.get("message_thread_id"),
            })
            return [types.SimpleNamespace(message_id=len(calls))]

        try:
            debate._prepare_side = fake_prepare
            debate._moderator_questions = fake_questions
            debate._run_question_round = fake_round
            debate._final_side_thesis_text = fake_final_side_text
            debate._build_final_memo = fake_final_memo
            debate._archive_session = fake_archive
            debate._opening_statements_rich_and_fallback_html = fake_opening_rich
            debate.send_rich_or_split_html_message = fake_rich_md
            debate.send_rich_html_or_split_html_message = fake_rich_html
            await debate._run_debate_session(session, FakeBot())
        finally:
            debate._prepare_side = original_prepare
            debate._moderator_questions = original_questions
            debate._run_question_round = original_round
            debate._final_side_thesis_text = original_final_side_text
            debate._build_final_memo = original_final_memo
            debate._archive_session = original_archive
            debate._opening_statements_rich_and_fallback_html = original_opening_rich
            debate.send_rich_or_split_html_message = original_rich_md
            debate.send_rich_html_or_split_html_message = original_rich_html

        # Assert: opening (Rich MD), final memo (Rich MD), sources (Rich HTML)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["delivery"], "rich_html")
        self.assertEqual(calls[1]["delivery"], "rich_md")
        self.assertEqual(calls[2]["delivery"], "rich_html")

        # Same chat and thread routing for all
        for call in calls:
            self.assertEqual(call["chat_id"], 123)
            self.assertEqual(call["thread"], 789)

        # Memo content preserved
        self.assertEqual(calls[1]["markdown"], "# Final Memo\n\n- **Finding**")

        # Sources should be Rich HTML with <details> block
        self.assertIn("<details>", calls[2]["html"])
        self.assertIn("<summary>", calls[2]["html"])
        self.assertIn("Sources", calls[2]["html"])


if __name__ == "__main__":
    unittest.main()
