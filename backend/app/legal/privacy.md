# What we collect

- Account: your email address (the one you sign up with, or, if you sign up with GitHub or Google, the verified address that provider gives us), a one-way hash of your password if you set one (Argon2id; we cannot read your password), whether and when you verified your email, and, if you sign in with GitHub or Google, that provider's account number for you.
- Sign-in: one cookie holds your session. It is HttpOnly, sent only over HTTPS in production, and lasts up to 180 days. Visitors who are not signed in also get this cookie. It holds only a random ID and a secret that proves the cookie is yours, not your name or email.
- Questions and answers: the full text of what you ask and the answers you get, with their citations. An answer to a common question can be reused, for up to 24 hours, for anyone who asks the same question; the reused copy does not record who asked.
- Feedback you choose to send: ratings, reasons, and anything you type in a comment or a "request a source" note.
- Usage: a record of each answered question against your allowance, and a record of each paid model call (for cost control), both linked to your account.
- Shared answers (Pro): if you share an answer, a copy of that question and answer, with its citations, is stored and shown on a public page to anyone who has the link, until you stop sharing it.
- Settings you choose: whether you get the weekly digest, the pages or terms you watch for changes, and the names of any API keys you create. We store only a one-way hash of each key.
- Billing (Pro only): our payment provider's customer and subscription identifiers for your account. Your card details go to the payment provider, not to us.

# What we don't collect

No advertising or analytics trackers. Fonts and scripts are served from our own domain. For rate limiting we use a coded form of your IP address, never the address itself. Our web server's access log does record IP addresses; our host, Fly.io, keeps it for 7 days.

# Who else receives data

- Hosting and storage: Fly.io (servers), Neon (database), Upstash (rate-limit counters).
- Answering: your question, and earlier questions in the same conversation when you ask a follow-up, are sent to the AI provider that writes the answer and to the provider that turns them into a search vector. Today both are Google (Gemini), which keeps API requests for 55 days to detect abuse. If Gemini is unavailable, OpenRouter writes the answer, using only providers that have a zero data retention policy and do not collect the text. We don't send your account id, email or IP address to them.
- Email: Resend sends sign-in links and security notices to your address, and, if you turn them on, the weekly digest and watch alerts.
- Payments: our payment provider receives your email address and an internal account id when you upgrade, and handles the payment itself. It also collects your billing details, such as your country for tax, and keeps its own records under its own privacy policy.
- Sign-in: GitHub or Google, only if you choose to sign in with them.

Our servers, database and rate-limit counters are in the United States (US East). If you use CiteVyn from another country, your data is transferred to and stored in the United States. CiteVyn is run from India, so the operator also accesses it from there.

# How long we keep it

Today, conversations, feedback and usage records are kept until the account is deleted; there is no automatic deletion yet. A conversation shows in your History, and can be exported, for 24 hours after it starts; after that, or once you close it, it is hidden from you but not erased. Records of paid model calls, a security log of sign-ins and answered questions, and our payment-event log are kept after an account is deleted, without your email. Shared answers stay public until you stop sharing them or your account is deleted. If you ask us to delete your account, we delete its data within 30 days, apart from those records. We also keep a one-way code made from your email address and the number of free-trial questions the account used, so the same address cannot start a new free trial.

If you use CiteVyn without an account, your questions are stored under the random ID in your cookie and kept the same way. No email is attached, so we can find them only if you tell us that ID, or sign up or sign in from the same browser, which moves them into your account.

# Your choices

- Export the conversations your History shows (up to the newest 500) as JSON or Markdown from the History drawer.
- Ask us to delete your account and its data by emailing rohit@stackclimb.com. There is no self-service delete button yet.
- Ask for a copy of your data, or for something in it to be corrected, by emailing rohit@stackclimb.com. We reply within 30 days. Depending on where you live, the law may give you further rights; email us to use them.

# Contact

CiteVyn is operated by Rohit Agrawal, Patna, Bihar, India. Privacy questions: rohit@stackclimb.com.
