# Our promise

We don't use your questions, answers or feedback to train AI models. We read feedback to find wrong or missing answers and to improve the documentation we answer from; it is not used to train any model.

# Our providers

Your questions are sent to the AI provider that writes each answer. Whether that provider may use them is set by its terms for the kind of account we use:

- Google Gemini (the main provider): [OWNER: confirm the API key's Google Cloud project has billing turned on, then keep this sentence: "We use the paid Gemini API." On the free tier Google does use prompts, so this must be checked first (issue #492).] Under Google's Gemini API terms (ai.google.dev/gemini-api/terms), Google does not use prompts or responses from paid services to improve its products. Google keeps them for 55 days to detect and prevent abuse of its usage policy (ai.google.dev/gemini-api/docs/usage-policies), and for any legal or regulatory disclosures it is required to make.
- OpenRouter (the backup): every request we send asks OpenRouter to use only providers that have a zero data retention policy (openrouter.ai/docs/features/zdr) and do not collect the text, so those providers neither store nor train on your questions. OpenRouter itself stores prompt text only if an account turns on input and output logging. [OWNER: confirm logging is off in OpenRouter's privacy settings.]

Until these are confirmed, this page is a draft and must not be published.

# What we keep

See the Privacy Policy for what we store and for how long.
