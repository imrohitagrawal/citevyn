/**
 * LandingPage — Main app shell for CiteVyn.
 *
 * Manages theme, view (landing vs chat), and all section logic.
 * Replaces the old multi-style architecture with a single unified page.
 */

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
    removeToast,
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
          />
        </div>
      )}

      <ToastHost toasts={toasts} onDismiss={removeToast} />
    </>
  );
}