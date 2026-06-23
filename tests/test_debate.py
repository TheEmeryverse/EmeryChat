import os
import sys
import tempfile
import types
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if "telegram" not in sys.modules:
    telegram_module = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    telegram_module.InlineKeyboardButton = InlineKeyboardButton
    telegram_module.InlineKeyboardMarkup = InlineKeyboardMarkup
    sys.modules["telegram"] = telegram_module


from emery import debate
import emery.globals as globals


class FakeBot:
    def __init__(self):
        self.messages = []

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

        async def fake_research(*args, **kwargs):
            return {"summary": "progressive and flat tax positions"}

        async def fake_query(*args, **kwargs):
            return (
                '{"pro_name":"Progressive Taxers","anti_name":"Flat Taxers",'
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
        self.assertIn("Progressive Taxers", debate._position_text(session))
        self.assertIn("Flat Taxers", debate._position_text(session))

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
            debate._fetch_source_content = fake_fetch
            debate._summarize_source = fake_summarize
            debate._summarize_research_packet = fake_packet
            packet = await debate._clerk_research(session, "pro", "side_deep", "question", max_sources=3)
        finally:
            debate._plan_clerk_queries = original_plan
            debate._search_web = original_search
            debate._fetch_source_content = original_fetch
            debate._summarize_source = original_summarize
            debate._summarize_research_packet = original_packet

        self.assertEqual(len(session.sources), 3)
        self.assertEqual(len(packet["source_ids"]), 3)
        self.assertEqual(packet["source_limit"], 3)

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
        original_final_memo = debate._build_final_memo
        original_appendix = debate._build_source_appendix
        original_archive = debate._archive_session
        observed_active = []

        async def fake_prepare(target, side):
            observed_active.append(globals.has_active_foreground_loops())

        async def fake_questions(target):
            return ["Q1", "Q2", "Q3"]

        async def fake_round(target, round_number, question):
            target.round_results.append({"round": round_number, "winner": "tie"})

        async def fake_final_side(target, side):
            return None

        async def fake_final_memo(target):
            return "Final memo"

        def fake_appendix(target):
            return "Sources"

        def fake_archive(target):
            return None

        try:
            debate._prepare_side = fake_prepare
            debate._moderator_questions = fake_questions
            debate._run_question_round = fake_round
            debate._final_side_thesis = fake_final_side
            debate._build_final_memo = fake_final_memo
            debate._build_source_appendix = fake_appendix
            debate._archive_session = fake_archive
            await debate._run_debate_session(session, FakeBot())
        finally:
            debate._prepare_side = original_prepare
            debate._moderator_questions = original_questions
            debate._run_question_round = original_round
            debate._final_side_thesis = original_final_side
            debate._build_final_memo = original_final_memo
            debate._build_source_appendix = original_appendix
            debate._archive_session = original_archive

        self.assertEqual(observed_active, [True, True])
        self.assertFalse(globals.has_active_foreground_loops())
        self.assertEqual(session.status, "completed")


if __name__ == "__main__":
    unittest.main()
