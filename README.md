# FlaskGPT

A Flask chat assistant with conversation domains, SQLite conversation history, source excerpts, response ratings, exports, PDF summaries, and basic CSV/SQLite data-quality reports.

## Setup

1. Install Python 3.13, Poetry, and Node.js.
2. Install Python dependencies with `poetry install`.
3. Copy `.env.example` to `.env` and add your Groq API key to `.env`.
4. Start the app with `poetry run python app.py`.
5. Open http://127.0.0.1:5000.

The `.env` file and local SQLite database are excluded from Git. Token counts may be estimates, and the data-quality score is a basic heuristic.
