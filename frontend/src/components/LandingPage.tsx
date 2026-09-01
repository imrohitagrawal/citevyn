/**
 * LandingPage — Main app shell for CiteVyn.
 *
 * Manages theme, view (landing vs chat), and all section logic.
 * Replaces the old multi-style architecture with a single unified page.
 */

import { useEffect } from "react";
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

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface LandingPageProps {
  theme: "light" | "dark";
  onThemeChange: (theme: "light" | "dark") => void;
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

  // ADR-0004 PR 12: the OAuth return trip is a hard navigation (the backend
  // redirects here with ?auth=ok or ?auth=error after the provider round
  // trip), so there is no client-side promise to await -- this reads the
  // query param the backend attaches instead. Confirmed: the frontend's
  // FIRST use of URLSearchParams/History API anywhere, kept minimal and
  // contained on purpose. bootstrapAuth()'s normal GET /v1/auth/me call
  // (fired on this same mount, via useAuth) already picks up the new
  // cookie with no special-casing here, since the cookie is set
  // server-side before the redirect lands.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const authResult = params.get("auth");
    if (authResult === "ok") {
      addToast({ kind: "success", title: "Signed in", message: "Welcome to CiteVyn." });
    } else if (authResult === "error") {
      addToast({ kind: "error", title: "Sign-in failed", message: "Sign-in failed. Try again." });
    }
    if (authResult !== null) {
      window.history.replaceState(null, "", window.location.pathname);
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
    </>
  );
}