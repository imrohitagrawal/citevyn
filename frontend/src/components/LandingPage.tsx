/**
 * LandingPage — Main app shell for CiteVyn.
 *
 * Manages theme, view (landing vs chat), and all section logic.
 * Replaces the old multi-style architecture with a single unified page.
 */

import { lazy, Suspense, useEffect, useState } from "react";
import { useLandingState } from "../hooks/useLandingState";
import { KB } from "../data/knowledgeBase";
import { Header } from "./Header";
import { Hero } from "./Hero";
import {
  QuestionTicker,
  SourcesStrip,
  Personas,
  HowItWorks,
  WhyDifferent,
  InteractiveDemo,
  Pricing,
  FAQ,
  CTABanner,
  Footer,
} from "./landing-sections";
import { ChatView } from "./ChatView";
import { ToastHost } from "./ToastHost";

// ADR-0004 PR 14: mounted only on the ?auth=ok return trip, and lazy, so the
// eager bundle carries just this line and the mount condition below.
const PasswordNudge = lazy(() => import("./Nudge"));

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface LandingPageProps {
  theme: "light" | "dark";
  onThemeChange: (theme: "light" | "dark") => void;
}

/** Query params THIS app attaches on the OAuth login / account-linking return trips. */
const OWN_QUERY_PARAMS = ["auth", "connect", "reason", "provider"] as const;

const PROVIDER_LABELS: ReadonlyMap<string, string> = new Map([
  ["github", "GitHub"],
  ["google", "Google"],
]);

function connectErrorMessage(reason: string | null, label: string | undefined): string {
  if (reason === "already_linked") {
    return label
      ? `${label} is already connected to a different CiteVyn account.`
      : "That account is already connected to a different CiteVyn account.";
  }
  if (reason === "denied") {
    return "Connection cancelled. Nothing was changed.";
  }
  if (reason === "session") {
    return "Your sign-in is too old to connect an account. Sign out, sign in again, then retry.";
  }
  return label ? `Couldn't connect ${label}. Try again.` : "Couldn't connect the account. Try again.";
}

export function LandingPage({ theme, onThemeChange }: LandingPageProps) {
  const {
    state,
    heroRef,
    onHeroInput,
    onChatInput,
    onChatKey,
    submitChat,
    onFocusHero,
    onHeroKey,
    onAskHero,
    getPro,
    goSection,
    enterChat,
    heroItem,
    heroPlaceholder,
    heroDots,
    marqueeItems,
    demoQuestions,
    heroChips,
    chatView,
    chatSuggestions,
    openFaq,
    toggleFaq,
    backToLanding,
    screen,
    live,
    toasts,
    addToast,
    removeToast,
    resumeSession,
  } = useLandingState();

  const dark = theme === "dark";
  // Captured ONCE, before the effect below strips the app's own query keys
  // (a lazy initializer, so it survives the replaceState). A magic-link or
  // OAuth login both land on ?auth=ok; whether to actually show the nudge is
  // PasswordNudge's own decision once the identity has resolved.
  const [passwordlessReturn] = useState(() => new URLSearchParams(window.location.search).get("auth") === "ok");

  // ADR-0004 PR 12: the OAuth return trip is a hard navigation (the backend
  // redirects here with ?auth=ok or ?auth=error after the provider round
  // trip), so there is no client-side promise to await -- this reads the
  // query param the backend attaches instead. Confirmed: the frontend's
  // FIRST use of URLSearchParams/History API anywhere, kept minimal and
  // contained on purpose. bootstrapAuth()'s normal GET /v1/auth/me call
  // (fired on this same mount, via useAuth) already picks up the new
  // cookie with no special-casing here, since the cookie is set
  // server-side before the redirect lands.
  //
  // ADR-0004 PR 13 adds a second family of self-attached params for the
  // account-linking round trip (?connect=ok|error&reason=...&provider=...).
  // Each family is checked and toasted INDEPENDENTLY (not one if/else-if
  // chain), and the cleanup removes only this app's OWN keys, reconstructing
  // the URL around anything else -- the original replaceState(pathname)
  // stripped the whole query string, which would have silently dropped an
  // unrelated param (a UTM tag, a future deep link) sharing this code path.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authResult = params.get("auth");
    if (authResult === "ok") {
      addToast({ kind: "success", title: "Signed in", message: "Welcome to CiteVyn." });
    } else if (authResult === "error") {
      addToast({ kind: "error", title: "Sign-in failed", message: "Sign-in failed. Try again." });
    }

    const connectResult = params.get("connect");
    if (connectResult === "ok" || connectResult === "error") {
      // `provider` is attacker-controllable via the URL bar; it is only ever
      // used as a key into a Map (NOT a plain object -- `?provider=constructor`
      // would otherwise walk Object.prototype and render a function body),
      // never rendered raw.
      const label = PROVIDER_LABELS.get(params.get("provider") ?? "");
      if (connectResult === "ok") {
        addToast({
          kind: "success",
          title: "Connected",
          message: label ? `${label} is now connected to your account.` : "Account connected.",
        });
      } else {
        addToast({ kind: "error", title: "Couldn't connect", message: connectErrorMessage(params.get("reason"), label) });
      }
    }

    if (authResult !== null || connectResult !== null) {
      for (const key of OWN_QUERY_PARAMS) params.delete(key);
      const rest = params.toString();
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${rest ? `?${rest}` : ""}${window.location.hash}`,
      );
    }
    // Runs once on mount only -- addToast is stable (useCallback in
    // useToast) and re-running this on every re-render would re-fire the
    // toast on unrelated state changes.
  }, [addToast]);

  // ADR-0004 PR 9: confirm the claim-on-login the backend already performs
  // (PR 6) transparently via the session cookie -- no client-side refetch
  // is needed for the CURRENT tab, since the session_id never changes,
  // only its owner. This toast is the one thing that genuinely needed
  // wiring: the auth module has no visibility into chat state, so it
  // cannot know on its own whether there was anything to save.
  const handleAuthenticated = (hadChatHistory: boolean) => {
    addToast(
      hadChatHistory
        ? {
            kind: "success",
            title: "Signed in",
            message: "Your conversation is saved to your account.",
          }
        : { kind: "success", title: "Signed in", message: "Welcome to CiteVyn." },
    );
  };

  return (
    <>
      {screen === "landing" && (
        <>
          <Header
            themeLabel={dark ? "LIGHT" : "DARK"}
            themeGlyph={dark ? "☀" : "☾"}
            onThemeToggle={() => onThemeChange(dark ? "light" : "dark")}
            onAskClick={() => enterChat(null)}
            onNavClick={goSection}
            hasChatHistory={state.messages.length > 0}
            onAuthenticated={handleAuthenticated}
            onResumeSession={resumeSession}
          />

          {/* Landing View */}
          <main id="top" data-screen-label="Landing">
        <Hero
          heroInput={state.heroInput}
          heroPlaceholder={heroPlaceholder}
          heroNudge={state.heroNudge}
          heroBoxShake={state.heroNudge}
          heroRef={heroRef}
          onHeroInput={onHeroInput}
          onHeroKey={onHeroKey}
          onAskHero={onAskHero}
          onFocusHero={onFocusHero}
          heroChips={heroChips}
          hero={{
            q: heroItem.q,
            text: state.hero.text,
            streaming: state.hero.streaming,
            showSources: state.hero.showSources,
            sources: heroItem.sources,
          }}
          heroDots={heroDots}
        />

        <QuestionTicker marquee={[...marqueeItems, ...marqueeItems]} />

        <SourcesStrip />

        <Personas onAsk={(q) => enterChat(q)} />

        <HowItWorks />

        <WhyDifferent />

        <InteractiveDemo
          demoQuestions={demoQuestions}
          demo={{
            // The question shown ABOVE the answer must be the question the answer
            // is for — i.e. the selected demo (state.demo.key), NOT the hero's
            // auto-rotating placeholder (heroItem.q). Binding it to heroItem.q let
            // the header cycle independently of the answer, so the panel showed a
            // question that didn't match its answer (looks like it answered the
            // wrong thing — the opposite of the "trustworthy" promise).
            q: KB[state.demo.key]?.q ?? "",
            text: state.demo.text,
            streaming: state.demo.streaming,
            done: state.demo.done,
            showSources: Boolean(state.demo.done && !state.demo.refusal && (KB[state.demo.key]?.sources?.length ?? 0) > 0),
            refusal: state.demo.refusal,
            sources: state.demo.key ? (KB[state.demo.key]?.sources || []) : [],
          }}
          onOpenChat={() => enterChat(null)}
        />

        <Pricing onGetPro={getPro} onOpenChat={() => enterChat(null)} />

        <FAQ openFaq={openFaq} toggleFaq={toggleFaq} />

        <CTABanner onOpenChat={() => enterChat(null)} />

        <Footer />
      </main>
        </>
      )}

      {/* Chat View */}
      {screen === "chat" && (
        // Full-viewport flex column: the header takes its natural height (which
        // varies — it wraps taller on narrow screens) and the chat pane fills
        // the rest, so the message list (not the page body) is always the
        // scroller regardless of header height. 100dvh tracks the mobile
        // browser's dynamic viewport (URL bar / keyboard).
        <div className="chat-screen">
          <Header
            themeLabel={dark ? "LIGHT" : "DARK"}
            themeGlyph={dark ? "☀" : "☾"}
            onThemeToggle={() => onThemeChange(dark ? "light" : "dark")}
            onAskClick={() => enterChat(null)}
            onNavClick={goSection}
            hasChatHistory={state.messages.length > 0}
            onAuthenticated={handleAuthenticated}
            onResumeSession={resumeSession}
          />
          <ChatView
            messages={chatView}
            chatEmpty={state.messages.length === 0}
            chatSuggestions={chatSuggestions}
            chatInput={state.chatInput}
            onChatInput={onChatInput}
            onChatKey={onChatKey}
            onSendClick={submitChat}
            onBackClick={backToLanding}
            live={live}
            pending={state.pending}
            highlightedIndex={state.highlight}
            sendTick={state.sendTick}
          />
        </div>
      )}

      <ToastHost toasts={toasts} onDismiss={removeToast} />
      {passwordlessReturn && (
        <Suspense fallback={null}>
          <PasswordNudge />
        </Suspense>
      )}
    </>
  );
}