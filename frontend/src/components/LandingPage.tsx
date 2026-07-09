/**
 * LandingPage — Main app shell for CiteVyn.
 *
 * Manages theme, view (landing vs chat), and all section logic.
 * Replaces the old multi-style architecture with a single unified page.
 */

import { useLandingState } from "../hooks/useLandingState";
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
    askHero,
    getPro,
    goSection,
    enterChat,
    heroItem,
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
  } = useLandingState();

  const dark = theme === "dark";

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
          />

          {/* Landing View */}
          <main id="top" data-screen-label="Landing">
        <Hero
          heroInput={state.heroInput}
          heroPlaceholder={[
            "Ask about Claude, Codex, Gemini…",
            "Does Claude Code cost money?",
            "How do I get a Gemini API key?",
            "What does --model do in Codex?",
            "Which Claude models are available?",
          ][state.phIndex]}
          heroNudge={state.heroNudge}
          heroBoxShake={state.heroNudge}
          heroRef={heroRef}
          onHeroInput={onHeroInput}
          onHeroKey={(e) => {
            if (e.key === "Enter") {
              const q = askHero();
              if (q) enterChat(q);
            }
          }}
          onAskHero={() => {
            const q = askHero();
            if (q) enterChat(q);
          }}
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
            q: heroItem.q,
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
        <>
          <Header
            themeLabel={dark ? "LIGHT" : "DARK"}
            themeGlyph={dark ? "☀" : "☾"}
            onThemeToggle={() => onThemeChange(dark ? "light" : "dark")}
            onAskClick={() => enterChat(null)}
            onNavClick={goSection}
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
          />
        </>
      )}
    </>
  );
}

// Stub imports for KB — used inline
import { KB } from "../data/knowledgeBase";