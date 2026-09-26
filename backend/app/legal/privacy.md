# What we collect

- Account: your email address (if you give one), a one-way hash of your password (Argon2id; we cannot read your password), whether and when you verified your email, and, if you sign in with GitHub or Google, that provider's account number for you. We do not store the provider's copy of your email separately.
- Sign-in: one cookie holds your session. It is HttpOnly, sent only over HTTPS in production, and lasts up to 180 days. Anonymous visitors get the same kind of cookie so their chat history can follow them if they sign up.
- Questions and answers: the full text of what you ask and the answers you get, with their citations, so you can see your history. Answers to common questions are cached for up to 24 hours and can be reused for anyone who asks the same question; the cache does not record who asked.
- Feedback you choose to send: ratings, reasons, and anything you type in a comment or a "request a source" note.
- Usage: a record of each answered question against your allowance, and a record of each paid model call (for cost control), both linked to your account.
- Billing (Pro only): Stripe's customer and subscription identifiers for your account. Your card details go to Stripe, not to us.

# What we don't collect

No advertising or analytics trackers. Fonts and scripts are served from our own domain. For rate limiting we use a salted, one-way code made from your IP address, never the address itself; our web server's access log does record IP addresses. [OWNER: how long logs are kept.]

# Who else receives data

- Hosting and storage: Fly.io (servers), Neon (database), Upstash (rate-limit counters).
- Answering: your question, and earlier questions in the same conversation when you ask a follow-up, are sent to the AI provider that writes the answer and to the provider that indexes it for search. Today that is Google (Gemini), with OpenRouter as a fallback. We don't send your account id, email or IP address to them.
- Email: Resend sends sign-in links and security notices to your address.
- Payments: Stripe receives your email address and an internal account id when you upgrade.
- Sign-in: GitHub or Google, only if you choose to sign in with them.

[OWNER: the regions each provider stores data in, and any transfer out of your users' region.]

# How long we keep it

Today, conversations, feedback and usage records are kept until the account is deleted; there is no automatic deletion yet. Closing a conversation hides it but does not erase it. Records of paid model calls are kept for cost accounting even after an account is deleted, linked only to the old account id. [OWNER: the retention periods you commit to.]

# Your choices

- Export your conversations as JSON or Markdown from the History drawer.
- Ask us to delete your account and its data: [OWNER: contact address]. There is no self-service delete button yet.
- [OWNER: rights under the laws that apply to your users, e.g. GDPR or CCPA, and how to exercise them.]

# Contact

[OWNER: legal entity name and privacy contact.]
