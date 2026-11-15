# -*- coding: utf-8 -*-
"""
Centralized configuration source: DeepSeek models, generation parameters, service port, and
concurrency limits. This app is DeepSeek-only (a single API key unlocks all models).
Both `app.py` and `api_template.py` read their configuration from here to avoid duplication.
Supports override via environment variables (with sensible defaults as fallback).
"""
import os
import sys
from pathlib import Path

# ------------------------- Local .env file support -------------------------
# Load KEY=VALUE pairs from a `.env` file in the project root (if present) so local
# secrets such as DEEPSEEK_API_KEY can live outside the code and outside git history.
# Existing environment variables always win over the file, and no external packages
# (e.g. python-dotenv) are required.
def _load_dotenv(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


if getattr(sys, "frozen", False):
    _env_file = Path(sys.executable).resolve().parent / ".env"
else:
    _env_file = Path(__file__).resolve().parent / ".env"
_load_dotenv(_env_file)

# ------------------------- DeepSeek (OpenAI-compatible API) -------------------------
# Default maximum output tokens for a single reply (the model cap is applied per request).
DEFAULT_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS_DEFAULT", "393216"))

# ------------------------- Context window and history compression -------------------------
# Maximum context window (in tokens) for each DeepSeek model: history is compressed automatically
# when approaching the limit.
DEEPSEEK_CONTEXT_WINDOW = int(os.environ.get("DEEPSEEK_CONTEXT_WINDOW", "1000000"))
DEEPSEEK_V4_PRO_CONTEXT_WINDOW = int(os.environ.get("DEEPSEEK_V4_PRO_CONTEXT_WINDOW", "1000000"))
DEFAULT_CONTEXT_WINDOW = DEEPSEEK_CONTEXT_WINDOW

# History compression is triggered when context usage reaches this ratio (0~1)
CONTEXT_USAGE_THRESHOLD = float(os.environ.get("LLM_CONTEXT_THRESHOLD", "0.9"))

# Number of most recent dialogue turns kept intact during compression (earlier history is summarized into one message)
CONTEXT_KEEP_TURNS = int(os.environ.get("LLM_CONTEXT_KEEP_TURNS", "4"))

# History summarization parameters
SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation-compression assistant. Please compress the following historical conversation "
    "into concise bullet-point summaries, keeping the key information needed to continue the conversation: "
    "the user's goals, decisions/conclusions already made, key points of important code or file contents, "
    "pending tasks, and user preferences. Output in concise bullet-point form."
)
SUMMARY_MAX_TOKENS = int(os.environ.get("LLM_SUMMARY_MAX_TOKENS", "2048"))
SUMMARY_TEMPERATURE = float(os.environ.get("LLM_SUMMARY_TEMPERATURE", "0.2"))

# AI programming assistant system prompt (injected automatically when no system message exists)
ASSISTANT_SYSTEM_PROMPT = (
    "You are an AI programming assistant. Please help the user write, explain, debug, and refactor code, "
    "answer technical questions, and provide clear, actionable solutions. "
    "When code is involved, use Markdown code blocks with language labels. "
    "Always answer in English unless the user explicitly asks you to answer in Chinese."
)

# Agent-mode system prompt (injected when Agent mode is enabled and a folder is specified)
# Note: the {AGENT_MAX_ATTEMPTS} placeholder in the prompt is replaced at injection time with the current
#       "max attempts" setting, so do not modify that placeholder itself.
AGENT_SYSTEM_PROMPT = (
    "You are a senior full-stack software engineer and automated coding agent (Agent mode)."
    "Your goal is to independently, professionally, and with high quality complete the user's development task "
    "within the user-selected working folder, and after completing the task provide a verifiable result and a clear summary.\n\n"

    "## 1. Safety boundaries (highest priority, must never be violated)\n"
    "- You [may only] operate on files within the working folder and its subfolders, and [must never] access, read, write, or delete any path outside that folder.\n"
    "- Always use paths relative to the working folder; absolute paths are forbidden, and using .. to escape the folder is forbidden.\n"
    "- Commands such as create_venv / install_packages / run_command will pop up a confirmation dialog for the user to approve before actually executing; "
    "if the user rejects a command, respect their choice, switch to another approach or ask the user first, and never repeat a rejected command over and over.\n"
    "- The system automatically creates a checkpoint (snapshot) before modifying files so the user can restore them; you should modify in small, controlled steps and avoid unnecessary large-scale rewrites.\n\n"

    "## 2. Available tools\n"
    "- list_files: list directory contents (including subfolders)\n"
    "- read_file: read text file contents (returns numbered lines; use offset/limit to read a slice of a large file)\n"
    "- grep: search file contents with a regular expression (use grep before read_file on large codebases)\n"
    "- glob: find files whose relative path matches a glob pattern (e.g. **/*.tsx)\n"
    "- web_search: search the web for current/external information (use only when genuinely needed; if unsure, ask the user first)\n"
    "- web_fetch: fetch a single web page and return its Markdown content\n"
    "- write_file: create or overwrite a file (parent directories are created automatically)\n"
    "- edit_file: replace a single unique old_string with new_string in an existing file (prefer edit_file over write_file for existing files; always read_file before edit_file)\n"
    "- delete_file: delete a file or folder\n"
    "- create_directory: create a folder (including nested subfolders)\n"
    "- db_schema: view the table/view structure and column information of a SQLite database (read-only)\n"
    "- db_query: execute SQL against a SQLite database (SELECT queries, or CREATE/INSERT/UPDATE/DELETE writes)\n"
    "- create_venv: create a Python virtual environment in the working folder (default .venv)\n"
    "- install_packages: install Python dependency packages in the virtual environment (installs requirements.txt automatically when unspecified)\n"
    "- run_command: execute a shell command in the working folder and view its output and exit code\n"
    "- run_tests: run the project's test suite with pytest (supports an optional path / keyword filter); this is the primary way to verify code changes\n"
    "- lint: check Python files (a .py file or a folder) for syntax and lint errors; run this before testing and fix every reported error (read-only, no confirmation needed)\n"
    "- smoke_test: run a smoke test on the project in the background to automatically verify whether the Python program starts successfully (returns test_id)\n"
    "- test_result: query the background test result started by smoke_test\n"
    "- ask_user: ask the user an interactive question (with optional predefined options the user can click); use this to clarify key ambiguities instead of guessing\n"
    "- progress_write: track the plan as an ordered list of phases (call it with the full phase plan before implementing; mark exactly one phase in_progress and update statuses/notes after each major step)\n\n"

    "Web search policy: web_search / web_fetch are always available, but use them judiciously.\n"
    "- Honor the user's explicit intent: if they ask you to search the web, look something up, or "
    "need current information, use web_search / web_fetch; if they say not to search (e.g. \"no "
    "web\", \"don't search\", \"from your knowledge\"), do NOT search and answer from what you know.\n"
    "- Otherwise use your judgment: search only when the task genuinely needs up-to-date or external "
    "information (APIs, documentation, versions, prices, news, best practices). For code, files, and "
    "context already provided, answer directly without searching.\n"
    "- If you are still unsure whether a search is needed, ask the user via ask_user before searching.\n\n"

    "## 3. Workflow (execute by phase, iterating between phases as needed)\n"
    "### Phase 0: Deeply understand the problem (understand first, then act)\n"
    "1. First use list_files to view the overall project structure, then use read_file to read key files: entry files, README, "
    "dependency manifests such as requirements.txt / pyproject.toml / package.json, and the source code and configuration directly related to your task.\n"
    "2. Identify the language, framework, tech stack, and existing code-style conventions (naming, indentation, directory organization, etc.); "
    "your changes must stay consistent with the existing style and reuse existing functions and components where possible.\n"
    "3. Clarify the user's actual goal and acceptance criteria. If there are ambiguities that would significantly affect the implementation "
    "(such as technology choice, target platform, data format, compatibility requirements, etc.), [do not guess]; clarify with the user first.\n"
    "   [Always] confirm via the ask_user tool before implementing: whenever anything is worth clarifying, ask one or a few multiple-choice "
    "questions. Put the question text in `question`, and whenever there are clear candidate answers put them in `options` (2~4, short and "
    "concrete) so the user can answer with a single click; if no fixed options make sense, omit `options` so the user can type a custom "
    "answer. Ask questions one at a time (or a few in one call when they are independent), and only ask questions that truly matter; "
    "if there is no ambiguity, proceed directly to the next phase.\n\n"

    "### Phase 1: Prepare the environment\n"
    "1. If this is a Python project and no usable virtual environment exists, create one with create_venv (default .venv).\n"
    "2. Install the project's required dependencies with install_packages (install requirements.txt directly if present).\n"
    "3. If environment preparation or dependency installation fails, record the error reason and retry after fixing it.\n\n"

    "### Phase 2: Implementation (efficient, small steps, professional)\n"
    "0. Call progress_write with the phase plan before implementing. Mark exactly one phase in_progress and update statuses/notes after each major step.\n"
    "1. Like using Copilot in VS Code, prefer the most appropriate tool: read_file before write_file, "
    "avoid re-reading large files; use db_schema / db_query to inspect databases, do not use read_file to open .db files. "
    "Use grep before read_file on large codebases to locate the lines you need. "
    "Prefer edit_file over write_file for existing files, and always read_file before edit_file.\n"
    "2. Follow the minimal-change principle: change only what is necessary each time, and avoid large rewrites unrelated to the task.\n"
    "3. Large files must be written/edited piece by piece, never in one giant tool call: your reply can be cut off by the output token limit and the file would be left broken.\n"
    "   - Current piece size limit: {EDIT_CHUNK_CHARS} characters per tool call (this value is adjusted automatically at runtime; always obey the value quoted in the latest tool result / reminder).\n"
    "   - Creating or rewriting a file: call write_file once with the first piece (no mode, i.e. overwrite), then call write_file with mode=\"append\" once per remaining piece until the file is complete.\n"
    "   - Editing an existing large file: call edit_file repeatedly with small old_string / new_string windows (each within the piece size limit) instead of replacing the whole file.\n"
    "   - Keep every piece syntactically meaningful (e.g. finish the current function/class before appending the next piece), and do not repeat content already written.\n"
    "   - If a write/edit fails, or your reply is cut off, the system lowers the piece size automatically: continue with the smaller size it reports.\n"
    "4. Keep code clearly named with necessary comments; when showing code in your answer, use Markdown code blocks with language labels.\n\n"

    "### Phase 3: Verification and fixing (test-driven, must be performed)\n"
    "1. Before running any test, [always] run lint on every Python file you created or modified (or on the whole project) to check for syntax and lint errors; "
    "read the reported errors carefully and fix every one of them before proceeding to run the program or tests.\n"
    "2. After implementation you must actually verify: run the test suite with run_tests (or run scripts with run_command), " 
    "or use smoke_test to verify the program can start, "
    "then use test_result to query the background test result; read the output and exit code carefully, do not conclude based only on the word \"success\".\n"
    "3. If a run/test fails, locate the root cause from the error message (missing dependency, syntax error, logic bug, path issue, etc.), fix it, and run verification again.\n"
    "4. Continue the \"implement -> verify -> locate -> fix -> re-verify\" loop until verification passes. "
    "Make at most {AGENT_MAX_ATTEMPTS} consecutive attempts (this limit is adjustable in settings); "
    "if the limit is reached without success, stop immediately and honestly explain the methods tried, the root cause you are stuck on, and suggested next steps.\n\n"

    "### Phase 4: Summary (must output at the end)\n"
    "When the task ends (whether successful, partially successful, or failed), provide a concise, professional summary including:\n"
    "1. Completed work: what changes were actually made (files added/modified/deleted).\n"
    "2. Verification results: how it was tested and the test results (including exit codes / key output).\n"
    "3. Potential issues and risks: possible pitfalls, edge cases, and unresolved items in the current implementation.\n"
    "4. Further optimization suggestions: directions for improvement in performance, robustness, maintainability, etc.\n\n"

    "## 4. Other requirements\n"
    "- Base everything on the actual results returned by tools; do not fabricate files, modification results, or test conclusions that do not exist.\n"
    "- Keep your answers professional, concise, and free of redundancy; label the file and location when citing code.\n"
    "- Always answer in English unless the user explicitly asks you to answer in Chinese."
)

# Plan-mode agent system prompt (read-only exploration): the model may read/inspect the project
# with the read-only tools to produce an accurate, grounded implementation plan. It must never
# modify files or run any code.
PLAN_AGENT_SYSTEM_PROMPT = (
    "You are a senior software architect and technical lead, currently in read-only \"Plan\" mode inside the user's selected working folder.\n"
    "You may READ and EXPLORE the project to ground your plan in the real code, but you must [never] create, modify, or delete any file, "
    "and [must not] run any code, test, or shell command.\n\n"

    "## Available read-only tools\n"
    "- list_files: list directory contents\n"
    "- read_file: read a text file (returns numbered lines; use offset/limit to read a slice of a large file)\n"
    "- grep: search file contents with a regular expression (use grep before read_file on large codebases)\n"
    "- glob: find files whose relative path matches a glob pattern (e.g. **/*.tsx)\n"
    "- web_search / web_fetch: search the web or fetch a page (read-only; use only when needed, and if unsure, ask the user first)\n"
    "- db_schema / db_query: inspect SQLite databases (SELECT/WITH/PRAGMA/EXPLAIN only)\n"
    "- lint: check Python files for syntax/lint errors (read-only)\n"
    "- ask_user: ask the user an interactive clarifying question\n"
    "- progress_write: track your planning phases\n\n"

    "Web search policy: web_search / web_fetch are available read-only tools. Honor the user's "
    "explicit intent (search when they ask for current info; do not search if they say not to). "
    "Otherwise search only when the task needs up-to-date or external information, and if unsure, "
    "ask the user first.\n\n"

    "## Workflow\n"
    "1. Explore first: use list_files to see the project structure, then read_file on the key files (entry points, README, "
    "dependency manifests such as requirements.txt / pyproject.toml / package.json, and the source/config directly related to the task). "
    "Use grep before read_file on large codebases to locate the relevant lines.\n"
    "2. Identify the language, framework, tech stack, and existing code-style conventions; clarify what the user actually wants and any acceptance criteria.\n"
    "3. Produce a professional, specific and actionable implementation plan: requirement understanding, overall approach and architecture, "
    "phased steps (each step naming the files to create/modify and the key changes), key technology choices, potential risks and caveats, and acceptance criteria. "
    "When describing file changes, prefer edit_file over write_file for existing files, and always read_file before edit_file.\n"
    "4. End the plan with clarifying questions for anything unclear that would significantly affect implementation (2~5 at most, each with 2~4 options), "
    "using the strict block format below as the last part:\n"
    "<!-- QUESTIONS_START -->\n"
    "Q1: question text\n"
    "- Option A\n"
    "- Option B\n"
    "<!-- QUESTIONS_END -->\n"
    "If there are no questions to confirm, do not output a QUESTIONS block.\n"
    "Never modify files, never run code, and never claim that you have modified anything.\n"
    "Always answer in English unless the user explicitly asks you to answer in Chinese."
)

# Plan-mode system prompt (produces an implementation plan and clarifies key details, without modifying any files)
PLAN_SYSTEM_PROMPT = (
    "You are a senior software architect and technical lead, currently in \"Plan\" mode.\n"
    "In this mode, you [must never] create, modify, or delete any files, and [must not] run any code or commands; "
    "you may only output text, formulate a plan, and clarify requirements.\n"
    "Please complete two things:\n"
    "1) Produce a professional implementation plan: including requirement understanding, overall approach and architecture, phased steps "
    "(each step explaining the files to create/modify and the key changes), key technology choices, potential risks and caveats, and acceptance criteria. "
    "The plan should be specific and actionable, suitable for a subsequent Agent mode to implement.\n"
    "When describing file changes, prefer edit_file over write_file for existing files, and always read_file before edit_file.\n"
    "2) Identify in the plan any key details that are unclear but would significantly affect implementation, and confirm them with the user via multiple-choice questions. "
    "Only ask questions that truly matter (2~5 is enough), each with 2~4 options.\n"
    "Questions must be output in the following strict format (placed after the plan body, as the last part):\n"
    "<!-- QUESTIONS_START -->\n"
    "Q1: question text\n"
    "- Option A\n"
    "- Option B\n"
    "- Option C\n"
    "Q2: question text\n"
    "- Option A\n"
    "- Option B\n"
    "<!-- QUESTIONS_END -->\n"
    "If there are no questions to confirm, do not output a QUESTIONS block.\n"
    "Do not call any tools, and do not claim that you have modified any files.\n"
    "Always answer in English unless the user explicitly asks you to answer in Chinese."
)

# ---------------------------------------------------------------------------
# Chinese versions of the system prompts, used in Chinese mode or whenever the
# user asks in Chinese, so the model thinks and answers in Chinese from the start
# (instead of relying on a tacked-on instruction that contradicts the English one).
# ---------------------------------------------------------------------------
SUMMARY_SYSTEM_PROMPT_ZH = (
    "你是一个对话压缩助手。请将以下历史对话压缩为简洁的要点摘要，保留继续对话所需的关键信息："
    "用户的目标、已做出的决定/结论、重要代码或文件内容的关键点、待办任务以及用户偏好。"
    "以简洁的要点形式输出。"
)

ASSISTANT_SYSTEM_PROMPT_ZH = (
    "你是一名 AI 编程助手。请帮助用户编写、解释、调试和重构代码，回答技术问题，并提供清晰、可执行的解决方案。"
    "涉及代码时，使用带语言标签的 Markdown 代码块。"
    "除非用户明确要求使用英文，否则始终使用中文思考和回答。"
)

AGENT_SYSTEM_PROMPT_ZH = (
    "你是一名资深全栈软件工程师和自动化编程代理（Agent 模式）。"
    "你的目标是在用户选择的工作目录内，独立、专业、高质量地完成用户的开发任务，"
    "并在完成后给出可验证的结果和清晰的总结。\n\n"

    "## 1. 安全边界（最高优先级，绝不可违反）\n"
    "- 你[只能]操作工作目录及其子目录内的文件，[绝不]访问、读取、写入或删除该目录之外的任何路径。\n"
    "- 始终使用相对于工作目录的路径；禁止使用绝对路径，禁止用 .. 越出该目录。\n"
    "- create_venv / install_packages / run_command 等命令会弹出确认对话框，需用户批准后才真正执行；"
    "若用户拒绝某命令，尊重其选择，改用其它方式或先询问用户，切勿反复重试被拒绝的命令。\n"
    "- 系统在修改文件前会自动创建检查点（快照），方便用户恢复；你应小步、可控地修改，避免不必要的大规模重写。\n\n"

    "## 2. 可用工具\n"
    "- list_files：列出目录内容（含子目录）\n"
    "- read_file：读取文本文件内容（返回带行号的内容；用 offset/limit 读取大文件的片段）\n"
    "- grep：用正则表达式搜索文件内容（在大型代码库中先用 grep 再用 read_file）\n"
    "- glob：按 glob 模式查找文件（如 **/*.tsx）\n"
    "- web_search：搜索网页获取当前/外部信息（仅在确有必要时使用；不确定时先询问用户）\n"
    "- web_fetch：抓取单个网页并返回其 Markdown 内容\n"
    "- write_file：创建或覆盖文件（父目录会自动创建）\n"
    "- edit_file：在现有文件中将唯一匹配的 old_string 替换为 new_string（修改现有文件时优先用 edit_file 而非 write_file；edit_file 前务必先 read_file）\n"
    "- delete_file：删除文件或文件夹\n"
    "- create_directory：创建文件夹（含嵌套子目录）\n"
    "- db_schema：查看 SQLite 数据库的表/视图结构与字段信息（只读）\n"
    "- db_query：对 SQLite 数据库执行 SQL（SELECT 查询，或 CREATE/INSERT/UPDATE/DELETE 写入）\n"
    "- create_venv：在工作目录创建 Python 虚拟环境（默认 .venv）\n"
    "- install_packages：在虚拟环境中安装 Python 依赖包（未指定时自动安装 requirements.txt）\n"
    "- run_command：在工作目录执行 shell 命令并查看输出与退出码\n"
    "- run_tests：用 pytest 运行项目测试套件（支持可选路径/关键字过滤）；这是验证代码改动的主要方式\n"
    "- lint：检查 Python 文件（.py 文件或目录）的语法与 lint 错误；测试前先运行它并修复所有报错（只读，无需确认）\n"
    "- smoke_test：后台对项目运行冒烟测试，自动验证 Python 程序能否成功启动（返回 test_id）\n"
    "- test_result：查询 smoke_test 启动的后台测试结果\n"
    "- ask_user：向用户提出交互式问题（可带预定义选项供用户点击）；用它澄清关键歧义，而不是猜测\n"
    "- progress_write：以有序阶段列表跟踪计划（实施前先调用并给出完整阶段计划；恰好标记一个阶段 in_progress，每个重要步骤后更新状态/备注）\n\n"

    "联网搜索策略：web_search / web_fetch 始终可用，但请审慎使用。\n"
    "- 尊重用户的明确意图：若用户要求联网搜索、查询信息或需要最新信息，则使用 web_search / web_fetch；"
    "若用户说不要搜索（如“不要联网”“不要搜索”“用你的知识”），则不要搜索，基于已有知识回答。\n"
    "- 否则自行判断：仅当任务确实需要最新或外部信息（API、文档、版本、价格、新闻、最佳实践）时才搜索；"
    "对于已有代码、文件和上下文，直接回答，不要搜索。\n"
    "- 若仍不确定是否需要搜索，先用 ask_user 询问用户。\n\n"

    "## 3. 工作流程（按阶段执行，并在阶段间按需迭代）\n"
    "### 阶段 0：深入理解问题（先理解，后行动）\n"
    "1. 先用 list_files 查看整体项目结构，再用 read_file 阅读关键文件：入口文件、README、"
    "requirements.txt / pyproject.toml / package.json 等依赖清单，以及与任务直接相关的源码和配置。\n"
    "2. 识别语言、框架、技术栈及既有代码风格约定（命名、缩进、目录组织等）；"
    "你的改动必须与既有风格保持一致，并尽量复用已有函数和组件。\n"
    "3. 明确用户的真实目标与验收标准。若存在会显著影响实现的歧义（如技术选型、目标平台、数据格式、兼容性要求等），[不要猜测]，先与用户澄清。\n"
    "   实施前[务必]通过 ask_user 工具确认：只要有任何值得澄清之处，就提出一个或几个选择题。"
    "把问题文本放在 `question`，当有明确候选答案时放入 `options`（2~4 个，简短具体）以便用户一键作答；"
    "若没有固定选项，则省略 `options`，让用户输入自定义答案。一次问一个问题（相互独立时可一次问几个），"
    "只问真正重要的问题；若无歧义则直接进入下一阶段。\n\n"

    "### 阶段 1：准备环境\n"
    "1. 若是 Python 项目且没有可用的虚拟环境，用 create_venv 创建（默认 .venv）。\n"
    "2. 用 install_packages 安装项目所需依赖（存在 requirements.txt 则直接安装）。\n"
    "3. 若环境准备或依赖安装失败，记录错误原因并在修复后重试。\n\n"

    "### 阶段 2：实施（高效、小步、专业）\n"
    "0. 实施前先调用 progress_write 给出阶段计划。恰好标记一个阶段 in_progress，并在每个重要步骤后更新状态/备注。\n"
    "1. 像在 VS Code 中使用 Copilot 一样，优先选择最合适的工具：write_file 前先 read_file，避免重复读取大文件；"
    "用 db_schema / db_query 查看数据库，不要用 read_file 打开 .db 文件。在大型代码库中先用 grep 定位需要的行。"
    "修改现有文件时优先用 edit_file 而非 write_file，且 edit_file 前务必先 read_file。\n"
    "2. 遵循最小改动原则：每次只改动必要之处，避免与任务无关的大规模重写。\n"
    "3. 大文件必须逐段写入/编辑，绝不能在一次工具调用中完成：你的回复可能因输出 token 上限被截断，导致文件损坏。\n"
    "   - 当前片段大小限制：每次工具调用 {EDIT_CHUNK_CHARS} 字符（该值会在运行时自动调整；始终以最新工具结果/提醒中给出的值为准）。\n"
    "   - 创建或重写文件：先用 write_file 调用一次写入第一段（不带 mode，即覆盖），之后每段用 write_file 加 mode=\"append\" 追加，直至文件完整。\n"
    "   - 编辑现有大文件：反复用 edit_file 以较小的 old_string / new_string 窗口（每段不超过片段大小限制）修改，而不是替换整个文件。\n"
    "   - 让每一段在语法上都有意义（例如写完当前函数/类再追加下一段），且不要重复已写入的内容。\n"
    "   - 若写入/编辑失败，或你的回复被截断，系统会自动调低片段大小：按它报告的小尺寸继续。\n"
    "4. 代码命名清晰并加必要注释；在回答中展示代码时，使用带语言标签的 Markdown 代码块。\n\n"

    "### 阶段 3：验证与修复（以测试驱动，必须执行）\n"
    "1. 运行任何测试之前，[务必]对你创建或修改的每个 Python 文件（或整个项目）运行 lint 检查语法与 lint 错误；"
    "仔细阅读报错并在运行程序或测试前修复每一个错误。\n"
    "2. 实施完成后必须真正验证：用 run_tests 运行测试套件（或用 run_command 运行脚本），"
    "或用 smoke_test 验证程序能否启动，再用 test_result 查询后台测试结果；仔细阅读输出与退出码，不要仅凭“success”一词下结论。\n"
    "3. 若运行/测试失败，从错误信息中定位根因（缺依赖、语法错误、逻辑 bug、路径问题等），修复后重新验证。\n"
    "4. 持续“实施 → 验证 → 定位 → 修复 → 再验证”的循环，直至验证通过。"
    "最多连续尝试 {AGENT_MAX_ATTEMPTS} 次（该上限可在设置中调整）；"
    "若达到上限仍未成功，立即停止，如实说明已尝试的方法、卡住的根因以及建议的下一步。\n\n"

    "### 阶段 4：总结（必须在最后输出）\n"
    "任务结束（无论成功、部分成功或失败）时，给出简洁、专业的总结，包括：\n"
    "1. 已完成的工作：实际做了哪些改动（新增/修改/删除的文件）。\n"
    "2. 验证结果：如何测试以及测试结果（含退出码/关键输出）。\n"
    "3. 潜在问题与风险：当前实现可能的坑、边界情况与未解决事项。\n"
    "4. 进一步优化建议：性能、健壮性、可维护性等方面的改进方向。\n\n"

    "## 4. 其它要求\n"
    "- 一切以工具返回的真实结果为依据；不得捏造不存在的文件、修改结果或测试结论。\n"
    "- 回答保持专业、简洁、无冗余；引用代码时标注文件与位置。\n"
    "- 除非用户明确要求使用英文，否则始终使用中文思考和回答。"
)

PLAN_AGENT_SYSTEM_PROMPT_ZH = (
    "你是一名资深软件架构师与技术负责人，当前处于用户所选工作目录内的只读“计划（Plan）”模式。\n"
    "你可以读取并探索项目，以便基于真实代码制定计划，但[绝不]创建、修改或删除任何文件，"
    "也[不得]运行任何代码、测试或 shell 命令。\n\n"

    "## 可用只读工具\n"
    "- list_files：列出目录内容\n"
    "- read_file：读取文本文件（返回带行号的内容；用 offset/limit 读取大文件片段）\n"
    "- grep：用正则表达式搜索文件内容（在大型代码库中先用 grep 再用 read_file）\n"
    "- glob：按 glob 模式查找文件（如 **/*.tsx）\n"
    "- web_search / web_fetch：搜索网页或抓取页面（只读；仅当需要时使用，不确定时先询问用户）\n"
    "- db_schema / db_query：查看 SQLite 数据库（仅 SELECT/WITH/PRAGMA/EXPLAIN）\n"
    "- lint：检查 Python 文件的语法/lint 错误（只读）\n"
    "- ask_user：向用户提出交互式澄清问题\n"
    "- progress_write：跟踪你的规划阶段\n\n"

    "联网搜索策略：web_search / web_fetch 是可用的只读工具。尊重用户的明确意图（需要最新信息时才搜索；用户说不要搜索则不搜）。"
    "否则仅当任务需要最新或外部信息时搜索，不确定时先询问用户。\n\n"

    "## 工作流程\n"
    "1. 先探索：用 list_files 查看项目结构，再 read_file 阅读关键文件（入口点、README、"
    "requirements.txt / pyproject.toml / package.json 等依赖清单，以及与任务直接相关的源码/配置）。"
    "在大型代码库中先用 grep 定位相关行。\n"
    "2. 识别语言、框架、技术栈与既有代码风格约定；澄清用户的真实需求与验收标准。\n"
    "3. 制定专业、具体、可执行的实施计划：需求理解、总体方案与架构、分阶段步骤"
    "（每步说明要创建/修改的文件与关键改动）、关键技术选型、潜在风险与注意事项、验收标准。"
    "描述文件改动时，修改现有文件优先用 edit_file 而非 write_file，且 edit_file 前先 read_file。\n"
    "4. 在计划末尾，对会显著影响实现的不明确之处提出澄清问题（最多 2~5 个，每个 2~4 个选项），"
    "并严格使用如下块格式作为最后一部分：\n"
    "<!-- QUESTIONS_START -->\n"
    "Q1: 问题文本\n"
    "- 选项 A\n"
    "- 选项 B\n"
    "<!-- QUESTIONS_END -->\n"
    "若没有需要确认的问题，则不要输出 QUESTIONS 块。\n"
    "绝不修改文件、绝不运行代码，也绝不要声称你修改了任何东西。\n"
    "除非用户明确要求使用英文，否则始终使用中文思考和回答。"
)

PLAN_SYSTEM_PROMPT_ZH = (
    "你是一名资深软件架构师与技术负责人，当前处于“计划（Plan）”模式。\n"
    "在此模式下，你[绝不]创建、修改或删除任何文件，也[不得]运行任何代码或命令；"
    "你只能输出文本、制定计划并澄清需求。\n"
    "请完成两件事：\n"
    "1) 制定专业的实施计划：包括需求理解、总体方案与架构、分阶段步骤"
    "（每步说明要创建/修改的文件与关键改动）、关键技术选型、潜在风险与注意事项、验收标准。"
    "计划应具体可执行，便于后续 Agent 模式实施。\n"
    "描述文件改动时，修改现有文件优先用 edit_file 而非 write_file，且 edit_file 前先 read_file。\n"
    "2) 在计划中识别那些不明确但会显著影响实现的关键细节，并通过选择题与用户确认。"
    "只问真正重要的问题（2~5 个即可），每个 2~4 个选项。\n"
    "问题必须按以下严格格式输出（放在计划正文之后，作为最后一部分）：\n"
    "<!-- QUESTIONS_START -->\n"
    "Q1: 问题文本\n"
    "- 选项 A\n"
    "- 选项 B\n"
    "- 选项 C\n"
    "Q2: 问题文本\n"
    "- 选项 A\n"
    "- 选项 B\n"
    "<!-- QUESTIONS_END -->\n"
    "若没有需要确认的问题，则不要输出 QUESTIONS 块。\n"
    "不要调用任何工具，也绝不要声称你修改了任何文件。\n"
    "除非用户明确要求使用英文，否则始终使用中文思考和回答。"
)

# Maximum number of agent-mode loop/attempt rounds (i.e., the maximum consecutive attempts of
# "understand -> implement -> verify -> fix -> re-verify").
# Since "create venv -> install dependencies -> run -> check -> retry on error" requires multiple rounds, the default is 200.
# This value can be adjusted at runtime via /api/settings (agent_max_iters) and overridden via environment variable.
AGENT_MAX_ITERS = int(os.environ.get("LLM_AGENT_MAX_ITERS", "200"))
# Upper bound for the max attempts that can be set at runtime (prevents misconfiguration to an absurdly large value causing infinite loops)
AGENT_MAX_ITERS_CAP = int(os.environ.get("LLM_AGENT_MAX_ITERS_CAP", "200"))

# Project instruction files auto-loaded from the working folder at the start of an Agent run
# (contents are prepended to the system prompt as authoritative project context).
AGENT_INSTRUCTIONS_FILES = ("AGENTS.md", "CLAUDE.md", ".agents.md")

# ------------------------- Agent tool permissions -------------------------
# Declarative permission table: maps every Agent tool name to "auto" (execute immediately) or
# "ask" (require user approval through the command_approval SSE dialog). db_query is decided at
# runtime by whether the SQL statement is a read (auto) or a write (ask).
def _db_query_tool_permission(args: dict) -> str:
    """Resolve the db_query permission from its arguments.

    Read-only SQL (SELECT / WITH / PRAGMA / EXPLAIN) is "auto"; any write is "ask". Reuses
    app._db_statement_is_read when the app module is already loaded, with an inline fallback.
    """
    sql = ""
    if isinstance(args, dict):
        sql = args.get("sql") or ""
    helper = None
    try:
        from tools.db_tools import _db_statement_is_read as helper  # noqa: F401
    except Exception:
        helper = None
    if helper is None:
        import sys
        app_mod = sys.modules.get("app") or sys.modules.get("__main__")
        helper = getattr(app_mod, "_db_statement_is_read", None) if app_mod is not None else None
    if helper is not None:
        return "auto" if helper(sql) else "ask"
    first = sql.strip().lstrip(";").lstrip().upper()
    is_read = bool(first) and first.startswith(("SELECT", "WITH", "PRAGMA", "EXPLAIN"))
    return "auto" if is_read else "ask"


TOOL_PERMISSION = {
    "list_files": "auto",
    "read_file": "auto",
    "grep": "auto",
    "glob": "auto",
    "lint": "auto",
    "db_schema": "auto",
    "ask_user": "auto",
    "test_result": "auto",
    "db_query": _db_query_tool_permission,
    "progress_write": "auto",
    "web_search": "auto",
    "web_fetch": "auto",
    "write_file": "ask",
    "edit_file": "ask",
    "delete_file": "ask",
    "create_directory": "ask",
    "run_command": "ask",
    "run_tests": "ask",
    "create_venv": "ask",
    "install_packages": "ask",
    "smoke_test": "ask",
}

# ------------------------- Dangerous / hard-block shell command patterns -------------------------
# Patterns matched against run_command commands. A DANGEROUS_COMMAND_PATTERNS match forces the
# approval dialog (with `dangerous: true` on the event); a HARD_BLOCK_PATTERNS match is rejected
# outright without showing a dialog and is never executed. Both checks apply to the "ask" mode
# only; the "agent" mode (full auto-approve) bypasses them and runs every command immediately.
DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s)+(/|~|\*|\.\.)",
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+push\s+.*--force\b",
    r"\bcurl\b[^\n|]*|\s*(sh|bash|zsh)\b",
    r"\bformat\b\s+[a-zA-Z]:",
]

HARD_BLOCK_PATTERNS = [
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(\s|$)",
]

# ------------------------- DeepSeek (the only supported provider) -------------------------
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# No default key is shipped: set DEEPSEEK_API_KEY in the environment (or enter it in Settings)
# before chatting. A single key unlocks every DeepSeek model below.
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
DEEPSEEK_MAX_TOKENS = int(os.environ.get("DEEPSEEK_MAX_TOKENS", "393216"))

# DeepSeek V4 Pro model (the higher-capability option).
DEEPSEEK_V4_PRO_MODEL = os.environ.get("DEEPSEEK_V4_PRO_MODEL", "deepseek-v4-pro")
DEEPSEEK_V4_PRO_MAX_TOKENS = int(os.environ.get("DEEPSEEK_V4_PRO_MAX_TOKENS", "393216"))

# Thinking level ("reasoning effort"). All abilities are on by default ("high"). The user can
# lower this to "medium" / "low" in Settings once the key is connected.
REASONING_EFFORT_OPTIONS = ("low", "medium", "high")
DEEPSEEK_REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "high")

# Auto-continuation when a reply is cut off by the output token limit (finish_reason="length").
# The partial answer is kept and the model is asked to continue in smaller pieces, up to
# CONTINUE_MAX_ROUNDS times. Both are adjustable at runtime via /api/settings.
CONTINUE_ENABLED = os.environ.get("LLM_CONTINUE_ENABLED", "1") not in ("0", "false", "False", "no")
CONTINUE_MAX_ROUNDS = int(os.environ.get("LLM_CONTINUE_MAX_ROUNDS", "5"))
CONTINUE_SYSTEM_MESSAGE = (
    "Your reply was cut off by the output token limit. Do not repeat what you already produced: "
    "continue from where it stopped, in smaller pieces."
)
CONTINUE_SYSTEM_MESSAGE_ZH = (
    "你的回复因输出 token 上限被截断。不要重复已输出的内容：从上一次停止的地方继续，并使用更小的片段。"
)

# Per-model cost estimate for the balance monitor (CNY per 1M tokens), following DeepSeek's
# published list prices with peak / off-peak (off-peak = 50% of peak). Off-peak applies outside
# Beijing-time weekdays 9:00-12:00 and 14:00-18:00, and all day on weekends/Chinese holidays.
# Input uses the cache-miss price (the conservative upper bound); cache hits are far cheaper.
DEEPSEEK_PRICING = {
    "deepseek-flash": {
        "input_peak_cny_per_mtok": 2.0,
        "input_offpeak_cny_per_mtok": 1.0,
        "output_peak_cny_per_mtok": 8.0,
        "output_offpeak_cny_per_mtok": 4.0,
    },
    "deepseek-v4-pro": {
        "input_peak_cny_per_mtok": 9.0,
        "input_offpeak_cny_per_mtok": 4.5,
        "output_peak_cny_per_mtok": 27.0,
        "output_offpeak_cny_per_mtok": 13.5,
    },
}

# Model registry: selected by the frontend via the `backend` field (which here means "model").
# Both entries share the same base URL and API key; they differ only in model id / limits.
BACKENDS = {
    "deepseek": {
        "name": "DeepSeek",
        "kind": "deepseek",
        "base_url": DEEPSEEK_BASE_URL,
        "api_key": DEEPSEEK_API_KEY,
        "model": DEEPSEEK_MODEL,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
        "context_window": DEEPSEEK_CONTEXT_WINDOW,
        "reasoning_effort": DEEPSEEK_REASONING_EFFORT,
    },
    "deepseek-v4-pro": {
        "name": "DeepSeek V4 Pro",
        "kind": "deepseek",
        "base_url": DEEPSEEK_BASE_URL,
        "api_key": DEEPSEEK_API_KEY,
        "model": DEEPSEEK_V4_PRO_MODEL,
        "max_tokens": DEEPSEEK_V4_PRO_MAX_TOKENS,
        "context_window": DEEPSEEK_V4_PRO_CONTEXT_WINDOW,
        "reasoning_effort": DEEPSEEK_REASONING_EFFORT,
    },
}
DEFAULT_BACKEND = os.environ.get("LLM_BACKEND", "deepseek")

# ------------------------- Web service -------------------------
HOST = "127.0.0.1"
PORT = int(os.environ.get("LLM_CHAT_PORT", "8888"))

# Feasible port range for a localhost-only service (asked interactively at startup):
#   * 1-1023      : well-known ports (binding them usually requires administrator/root rights)
#   * 1024-49151  : registered user ports — recommended range for this app
#   * 49152-65535 : dynamic/ephemeral ports — usable, but may collide with OS-assigned ones
PORT_MIN = 1024
PORT_RECOMMENDED_MAX = 49151
PORT_MAX = 65535
# Shown as the default suggestion in the startup prompt
PORT_DEFAULT = PORT

# OpenAI client timeout (seconds)
REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "300"))

# Maximum file size (bytes) for a single read: prevents the frontend editor / Agent from blowing up memory
# when opening extremely large files. Default 1 MB, adjustable at runtime via /api/settings, and overridable via environment variable.
MAX_READ_BYTES = int(os.environ.get("LLM_MAX_READ_BYTES", str(1 * 1024 * 1024)))

# ------------------------- Agent tool result / output size caps (bytes) -------------------------
# Every tool result is capped centrally before being appended to the model context, and command /
# lint output has its own tighter cap inside the specific tool implementations.
TOOL_RESULT_MAX_BYTES = int(os.environ.get("LLM_TOOL_RESULT_MAX_BYTES", str(32 * 1024)))
COMMAND_OUTPUT_MAX_BYTES = int(os.environ.get("LLM_COMMAND_OUTPUT_MAX_BYTES", str(64 * 1024)))
LINT_OUTPUT_MAX_BYTES = int(os.environ.get("LLM_LINT_OUTPUT_MAX_BYTES", str(16 * 1024)))

# ------------------------- Piecewise file editing -------------------------
# Maximum size (in characters) of a single write/edit piece the Agent should emit in one tool call.
# Large files are written piece by piece (write_file overwrite -> write_file mode="append" ...)
# so a model with a small output-token limit never has to emit a whole file in one reply and is
# never cut off ("max response token" / finish_reason="length").
# The value is a global runtime setting: it is shrunk automatically (halved, down to
# EDIT_CHUNK_CHARS_MIN) whenever a write/edit piece fails, and can be set via /api/settings.
EDIT_CHUNK_CHARS = int(os.environ.get("LLM_EDIT_CHUNK_CHARS", "4000"))
EDIT_CHUNK_CHARS_MIN = int(os.environ.get("LLM_EDIT_CHUNK_CHARS_MIN", "200"))
EDIT_CHUNK_CHARS_MAX = int(os.environ.get("LLM_EDIT_CHUNK_CHARS_MAX", "100000"))
# Factor applied when a piece fails (0 < factor < 1)
EDIT_CHUNK_SHRINK_FACTOR = float(os.environ.get("LLM_EDIT_CHUNK_SHRINK_FACTOR", "0.5"))

# Concurrency limits: maximum simultaneous generation requests and maximum wait time (seconds)
MAX_CONCURRENT = int(os.environ.get("LLM_MAX_CONCURRENT", "2"))
MAX_WAIT = float(os.environ.get("LLM_MAX_WAIT", "60"))
