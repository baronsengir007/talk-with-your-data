# 0006 - Chat with memory, React front end served by FastAPI

Status: accepted (user decision, 2026-10-29)

Context: a chatbot is a conversation; follow-ups and answers to clarifying questions need memory; the UX must be frictionless.

Decision: every message runs the same route with the last 3 turns as extra input; React (Vite) built to static files and served by the same FastAPI server.

Why: one server, one image; React keeps answer cards, steps, charts and buttons manageable. Reversing the front end costs about half a day; the analyst is unaffected.

Rejected: plain HTML and JavaScript (messy with this many parts); Next.js (a second server); Streamlit (little control over look and flow).
