# AI Concepts and Glossary

This page is an original, plain-language explainer of the everyday AI terms that come up
when using the tools CiteVyn covers (Claude, Claude Code, Codex, and Gemini). It is written
for a general audience — a product manager or a first-time user, not only engineers — so the
assistant can answer "what does this word mean?" questions with a grounded, cited source.

## What a large language model (LLM) is

A large language model, or LLM, is an AI system trained on a very large amount of text so it
can understand a request written in plain language and generate a useful text response. You
type a question or an instruction and the model replies with words — an answer, an
explanation, a summary, or code. LLMs power modern chat assistants and AI coding tools. They
work with probabilities rather than a fixed database of facts, which is why a good product
grounds their answers in real source documents and cites them, instead of trusting the model
to remember every detail. "LLM" and "large language model" mean the same thing.

## Which tools here are built on LLMs

All four products CiteVyn covers are built on large language models:

- Claude is a family of LLMs from Anthropic, used through the Claude API.
- Claude Code is Anthropic's agentic coding assistant, powered by Claude (an LLM); you can use
  it in your terminal and also as a desktop app, a web app, and IDE extensions.
- Codex is OpenAI's agentic coding tool, powered by an LLM to read and write code; it runs in
  the terminal (the Codex CLI) and also as a desktop app, an IDE extension, a cloud service,
  and inside ChatGPT.
- Gemini is Google's family of LLMs, used through the Gemini API.

So yes — Claude, Claude Code, Codex, and Gemini are all LLM-based AI tools. The difference is
mostly how you use each one (a raw API, or a coding agent you can run in the terminal and in
several other places, and so on), not whether it is a language model.

## What "a model" means and why there are different ones

When these tools mention "the model", they mean the specific version of the LLM handling your
request. Providers usually offer several models that trade off capability, speed, and cost:
a larger model is better at hard reasoning, while a smaller model is faster and cheaper for
simple, high-volume tasks. That is why you often pick a model — for example with a
model setting or a flag — to match the job. The exact list of available models and their
names lives in each provider's own documentation and changes over time.

## Grounded, cited answers

CiteVyn answers only from official documentation and attaches each claim to the exact source
page it came from. If the documents do not support a reliable answer, CiteVyn says so plainly
instead of guessing — because a language model on its own can sound confident while being
wrong.
