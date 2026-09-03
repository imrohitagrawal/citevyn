/**
 * Answer rendering: the markdown subset, citation chips and collapsed cards (#303).
 *
 * The escaping cases below are the mandatory adversarial pass. They assert the
 * structural property, not a sanitiser: this renderer never builds an HTML
 * string, so hostile input reaches the DOM as TEXT. If someone ever swaps the
 * data path for `dangerouslySetInnerHTML`, these go red.
 */
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AnswerBody, groupSources } from "./AnswerBody";
import type { Source } from "../data/knowledgeBase";

const src = (n: string, title: string, url: string): Source => ({ n, title, url });

describe("groupSources — collapse by document", () => {
  it("collapses repeated citations of one page into a single card listing every marker", () => {
    const groups = groupSources([
      src("1", "About CiteVyn", "/about"),
      src("2", "Install", "https://d/install"),
      src("3", "About CiteVyn", "/about"),
    ]);
    expect(groups).toEqual([
      { key: "/about", title: "About CiteVyn", url: "/about", markers: ["1", "3"] },
      { key: "https://d/install", title: "Install", url: "https://d/install", markers: ["2"] },
    ]);
  });

  it("orders markers numerically, not lexically", () => {
    const groups = groupSources([src("10", "A", "/a"), src("2", "A", "/a")]);
    expect(groups[0].markers).toEqual(["2", "10"]);
  });

  it("groups by title when a citation carries no URL", () => {
    const groups = groupSources([src("1", "Untitled doc", ""), src("2", "Untitled doc", "")]);
    expect(groups).toHaveLength(1);
    expect(groups[0].markers).toEqual(["1", "2"]);
  });
});

describe("AnswerBody — hostile input is never markup", () => {
  const hostile: Array<[string, string]> = [
    ["script tag", "<script>window.__pwned = 1</script>"],
    ["img onerror", '<img src=x onerror="window.__pwned = 1">'],
    ["iframe", '<iframe src="javascript:alert(1)"></iframe>'],
    ["svg onload", "<svg onload=alert(1)>"],
    ["entity-encoded script", "&lt;script&gt;alert(1)&lt;/script&gt;"],
    ["markdown link with a javascript scheme", "[click](javascript:alert(1))"],
  ];
  it.each(hostile)("renders %s as visible text and creates no element", (_l, payload) => {
    const { container } = render(<AnswerBody text={payload} sources={[]} />);
    expect(container.querySelectorAll("script, img, iframe, svg")).toHaveLength(0);
    expect(container.textContent).toBe(payload);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("renders the allowed subset as real elements, so the cases above are not vacuous", () => {
    const { container } = render(
      <AnswerBody text={"Use **npm** and `codex`\n- a bullet"} sources={[]} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe("npm");
    expect(container.querySelector("code")?.textContent).toBe("codex");
    expect(container.querySelectorAll("ul li")).toHaveLength(1);
  });
});

describe("AnswerBody — citation chips", () => {
  const sources = [src("1", "About CiteVyn", "/about"), src("2", "Install", "https://d/install")];

  it("links each marker to its source, in a new tab, titled with the source name", () => {
    render(<AnswerBody text="Grounded [1] and [2]." sources={sources} />);
    const chips = screen.getAllByRole("link");
    expect(chips.map((c) => c.getAttribute("href"))).toEqual(["/about", "https://d/install"]);
    expect(chips[0].getAttribute("target")).toBe("_blank");
    expect(chips[0].getAttribute("rel")).toBe("noopener noreferrer");
    expect(chips[0].getAttribute("title")).toBe("About CiteVyn");
  });

  it("keeps the plain [n] form in the text, so copying preserves it", () => {
    const { container } = render(<AnswerBody text="Grounded [1]." sources={sources} />);
    expect(container.querySelector(".message-body")?.textContent).toBe("Grounded [1].");
  });

  it("renders a marker with NO matching source as plain text, not a dead chip", () => {
    // Validation can drop a citation, and markers can be gapped.
    const { container } = render(<AnswerBody text="Claim [9]." sources={sources} />);
    expect(container.querySelectorAll(".citation-chip")).toHaveLength(0);
    expect(container.textContent).toContain("Claim [9].");
  });

  it("does not make a chip clickable when the URL is not a safe href", () => {
    const { container } = render(
      <AnswerBody text="Claim [1]." sources={[src("1", "Bad", "javascript:alert(1)")]} />,
    );
    expect(container.querySelector("a")).toBeNull();
    // Partner: it is still rendered as a chip, just inert.
    expect(container.querySelectorAll(".citation-chip")).toHaveLength(1);
  });
});

describe("AnswerBody — the chip/card tie", () => {
  const sources = [src("1", "About CiteVyn", "/about"), src("2", "Install", "https://d/install")];

  it("clicking a chip highlights the card it points at, and only that card", () => {
    const { container } = render(<AnswerBody text="A [1] B [2]." sources={sources} />);
    expect(container.querySelectorAll(".source-card.is-active")).toHaveLength(0);
    fireEvent.click(container.querySelector('.citation-chip[data-marker="1"]')!);
    const active = container.querySelectorAll(".source-card.is-active");
    expect(active).toHaveLength(1);
    expect(active[0].querySelector(".source-title")?.textContent).toBe("About CiteVyn");
  });

  it("hovering a card highlights its chips, and clears on leave", () => {
    const { container } = render(<AnswerBody text="A [1] B [2]." sources={sources} />);
    const card = container.querySelectorAll(".source-card")[1];
    fireEvent.mouseEnter(card);
    expect(container.querySelectorAll(".citation-chip.is-active")).toHaveLength(1);
    expect(container.querySelector(".citation-chip.is-active")?.getAttribute("data-marker")).toBe("2");
    fireEvent.mouseLeave(card);
    expect(container.querySelectorAll(".citation-chip.is-active")).toHaveLength(0);
  });

  it("highlights EVERY chip a collapsed card backs", () => {
    const { container } = render(
      <AnswerBody
        text="A [1] B [3]."
        sources={[src("1", "About CiteVyn", "/about"), src("3", "About CiteVyn", "/about")]}
      />,
    );
    fireEvent.mouseEnter(container.querySelector(".source-card")!);
    expect(container.querySelectorAll(".citation-chip.is-active")).toHaveLength(2);
  });
});

describe("AnswerBody — legend and streaming", () => {
  const sources = [src("1", "About CiteVyn", "/about")];

  it("shows the legend only when asked", () => {
    const { container, rerender } = render(
      <AnswerBody text="A [1]." sources={sources} showLegend />,
    );
    expect(container.querySelector(".citation-legend")?.textContent).toBe(
      "Numbers link each sentence to its source below.",
    );
    rerender(<AnswerBody text="A [1]." sources={sources} />);
    expect(container.querySelector(".citation-legend")).toBeNull();
  });

  it("holds back the cards and the legend until streaming finishes", () => {
    const { container } = render(
      <AnswerBody text="A [1]." sources={sources} streaming showLegend />,
    );
    expect(container.querySelectorAll(".source-card")).toHaveLength(0);
    expect(container.querySelector(".citation-legend")).toBeNull();
    expect(container.querySelector(".typing-cursor")).not.toBeNull();
    // ...but the chip is already live in the prose.
    expect(container.querySelectorAll(".citation-chip")).toHaveLength(1);
  });
});
