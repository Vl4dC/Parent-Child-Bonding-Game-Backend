// Learn -> quiz / flashcards. The page is a thin renderer: the server explains the topic
// for the learner's age, builds a quiz or a flashcard deck from that lesson (including
// "what goes in the hidden box?" diagram items), checks answers and tracks card reviews.
// Tournament mode (/api/tournament) is backend-only for now.
"use strict";

const $ = (id) => document.getElementById(id);
const KEYS = "ABCD";

let lesson = null;   // PublicLesson
let drawingPlanned = false; // the lesson's planned visuals are still being drawn
let quiz = null;     // PublicQuiz
let pos = 0;         // current question
let picks = [];      // chosen option index per question (or null)
let checks = [];     // CheckOut per question (or null)
let expired = false; // server forgot the quiz (restart); Next becomes "New topic"
let deck = null;     // PublicDeck
let cardState = null; // DeckState from the server
let flipped = false;
let cardsBack = "lesson";  // screen the flashcards' Back button returns to
let deckQuizId = null;     // set when the deck was made from a quiz's mistakes

// ---------------------------------------------------------------- helpers
function show(screen) {
  for (const id of ["start", "lesson", "quiz", "results", "cards"]) $(id).classList.toggle("hidden", id !== screen);
  window.scrollTo(0, 0);
}

function setError(node, msg) {
  node.textContent = msg || "";
  node.classList.toggle("hidden", !msg);
}

async function api(path, body, method) {
  const form = body instanceof FormData; // file uploads: let the browser set the multipart header
  const res = await fetch(path, {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: form ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : form ? body : JSON.stringify(body),
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const err = new Error(Array.isArray(d) ? d.map((e) => e.msg).join("; ") : d || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text; // never innerHTML: all text comes from a model
  return node;
}

async function busy(btn, label, work) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.replaceChildren(el("span", "spinner"), label);
  try {
    await work();
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ---------------------------------------------------------------- 0. the learner's own book (optional)
// A PDF or photos of pages are read on the server; the lesson, quiz, cards and pictures then use only those pages.
let material = null; // MaterialInfo

function describeBook(m) {
  const parts = [`${m.name}: ${m.page_count} page${m.page_count === 1 ? "" : "s"}`];
  if (m.vision_pages) parts.push(`${m.vision_pages} read from pictures`);
  let text = parts.join(", ") + ".";
  if (m.chapters.length) text += ` Chapters found: ${m.chapters.slice(0, 8).map((c) => c.title.split(":")[0]).join(", ")}.`;
  if (m.warnings.length) text += ` ${m.warnings.join(" ")}`;
  return text;
}

function renderBook() {
  $("book-status").textContent = material ? describeBook(material) : "";
  $("book-status").classList.toggle("hidden", !material);
  $("book-remove").classList.toggle("hidden", !material);
  $("topic-label").textContent = material
    ? "Which part of the book? A topic, “chapter 3” or “pages 40-55”"
    : "What do you want to learn?";
  $("topic").placeholder = material ? "e.g. photosynthesis, chapter 3, pages 40-55" : "e.g. photosynthesis, fractions, how the internet works";
}

$("book").addEventListener("change", async () => {
  const files = [...$("book").files];
  if (!files.length) return;
  setError($("book-error"), "");
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  $("book-status").replaceChildren(el("span", "spinner"), "Reading your pages… (scanned pages take longer)");
  $("book-status").classList.remove("hidden");
  $("book").disabled = true;
  try {
    material = await api("/api/materials", form);
  } catch (err) {
    material = null;
    setError($("book-error"), err.message);
  } finally {
    $("book").disabled = false;
    $("book").value = "";
    renderBook();
  }
});

$("book-remove").addEventListener("click", async () => {
  const gone = material;
  material = null;
  renderBook();
  try { await api(`/api/materials/${gone.id}`, undefined, "DELETE"); } catch { /* already gone: fine */ }
});

// ---------------------------------------------------------------- 1. learning
$("start-form").addEventListener("submit", (e) => {
  e.preventDefault();
  setError($("start-error"), "");
  busy($("learn"), material ? "Reading your book…" : "Writing your lesson…", async () => {
    try {
      lesson = await api("/api/lessons", {
        topic: $("topic").value,
        age: Number($("age").value),
        prefers_visuals: prefersVisuals(),
        material_id: material ? material.id : null,
      });
      // the lesson writer already decided which sections get a picture; draw those now
      drawingPlanned = lesson.sections.some((s) => s.visual);
      renderLesson();
      show("lesson");
      if (drawingPlanned) requestVisuals(null);
    } catch (err) {
      setError($("start-error"), err.message);
    }
  });
});

function renderLesson() {
  $("lesson-meta").textContent = `${lesson.topic} · explained for age ${lesson.age}` +
    (lesson.source ? ` · from ${lesson.source.title}, ${lesson.source.label}` + (lesson.source.truncated ? " (only the start fit)" : "") : "");
  $("lesson-title").textContent = lesson.title;
  $("lesson-summary").textContent = lesson.summary;
  $("lesson-sections").replaceChildren(
    ...lesson.sections.map((s, i) => {
      const div = el("div");
      div.dataset.section = i;
      div.append(el("h3", "", s.heading), ...s.body.split(/\n\s*\n/).map((p) => el("p", "", p)));
      return div;
    })
  );
  lesson.visuals.forEach(placeVisual);
  if (drawingPlanned) lesson.sections.forEach((s, i) => s.visual && placePending(i, s.visual));
  $("lesson-points").replaceChildren(...lesson.key_points.map((p) => el("li", "", p)));
  setError($("lesson-error"), "");
}

// ---------------------------------------------------------------- visuals
// Each visual is a standalone page from the server, shown in a sandboxed iframe
// (scripts allowed, but no access to this page). It reports its height via postMessage.
function visualFrame(html, title) {
  const frame = el("iframe");
  frame.sandbox = "allow-scripts";
  frame.title = title;
  frame.dataset.visual = "1"; // lets the height listener find it
  frame.srcdoc = html;
  return frame;
}

// Show `html` in `container` (or clear it). Skips the reload if it's already showing.
function showVisual(container, html, title) {
  if ((container.shownHtml || null) === (html || null)) return;
  container.shownHtml = html || null;
  container.replaceChildren(...(html ? [visualFrame(html, title)] : []));
}

function placeVisual(v) {
  const fig = el("figure", "visual");
  const frame = visualFrame(v.html, v.alt);
  const cap = el("figcaption");
  cap.append(el("strong", "", v.title), v.caption);
  fig.append(frame, cap);
  const section = $("lesson-sections").querySelector(`[data-section="${v.section_index}"]`);
  (section || $("lesson-sections")).append(fig);
  return fig;
}

// Placeholder where a lesson-planned visual will appear, so the text that refers to it makes sense
function placePending(sectionIndex, plan) {
  const fig = el("figure", "visual pending");
  fig.append(el("span", "spinner"), `Drawing a ${plan.kind}: ${plan.purpose}`);
  $("lesson-sections").querySelector(`[data-section="${sectionIndex}"]`).append(fig);
}

function clearPending() {
  drawingPlanned = false;
  for (const fig of document.querySelectorAll(".visual.pending")) fig.remove();
}

// Asking for a picture once tells us this learner likes visuals; later lessons lean visual
// without being asked. Browser storage can be unavailable, so it's best-effort.
function prefersVisuals() {
  try { return localStorage.getItem("prefersVisuals") === "1"; } catch { return false; }
}

function rememberPrefersVisuals() {
  try { localStorage.setItem("prefersVisuals", "1"); } catch { /* fine: just won't persist */ }
}

window.addEventListener("message", (e) => {
  if (!e.data || e.data.type !== "visual-height") return;
  for (const frame of document.querySelectorAll("iframe[data-visual]")) {
    if (frame.contentWindow === e.source) frame.style.height = `${Math.min(Math.max(e.data.height, 120), 900)}px`;
  }
});

async function requestVisuals(ask) {
  const forLesson = lesson.id;
  const status = $("visual-status");
  if (ask) {
    rememberPrefersVisuals();
    status.textContent = "Drawing that for you…";
    status.classList.remove("hidden");
  }
  try {
    const { visuals } = await api(`/api/lessons/${forLesson}/visuals`, ask ? { ask } : {});
    if (!lesson || lesson.id !== forLesson) return; // learner moved on
    if (!ask) clearPending();
    lesson.visuals.push(...visuals);
    const figs = visuals.map(placeVisual);
    if (ask && figs[0]) figs[0].scrollIntoView({ behavior: "smooth", block: "center" });
    status.textContent = ask && !visuals.length ? "Couldn't make a useful picture for that. Try rewording it." : "";
  } catch (err) {
    if (!lesson || lesson.id !== forLesson) return;
    if (!ask) clearPending();
    status.textContent = `Visuals unavailable: ${err.message}`;
  }
  status.classList.toggle("hidden", !status.textContent);
}

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const ask = $("ask").value.trim();
  if (!ask) return;
  $("ask").value = "";
  busy($("ask-btn"), "Drawing…", () => requestVisuals(ask));
});

$("new-topic").addEventListener("click", () => show("start"));

// ---------------------------------------------------------------- 2. quiz
$("make-quiz").addEventListener("click", () => {
  setError($("lesson-error"), "");
  busy($("make-quiz"), "Writing your quiz… (≈30 s)", async () => {
    try {
      beginQuiz(await api(`/api/lessons/${lesson.id}/quiz`, { num_questions: Number($("num").value) }));
    } catch (err) {
      setError($("lesson-error"), err.status === 404 ? "This lesson expired (the server restarted). Start a new topic." : err.message);
    }
  });
});

// Start answering `q` (a PublicQuiz): a fresh quiz from a lesson, or a follow-up on weak spots.
function beginQuiz(q) {
  quiz = q;
  pos = 0;
  expired = false;
  picks = quiz.questions.map(() => null);
  checks = quiz.questions.map(() => null);
  const n = quiz.dropped;
  $("dropped").textContent = `${n} question${n === 1 ? "" : "s"} removed after failing review.`;
  $("dropped").classList.toggle("hidden", !n);
  $("focus").textContent = quiz.focus.length ? `New questions on your weak spots: ${quiz.focus.join("; ")}.` : "";
  $("focus").classList.toggle("hidden", !quiz.focus.length);
  renderQuestion();
  show("quiz");
}

function renderQuestion() {
  const q = quiz.questions[pos];
  const total = quiz.questions.length;
  const check = checks[pos];
  const answered = checks.filter(Boolean).length;

  $("progress").textContent = `Q${pos + 1} / ${total}`;
  $("running").textContent = `Score ${checks.filter((c) => c && c.correct).length} / ${answered}`;
  $("bar").style.width = `${(answered / total) * 100}%`;
  $("question").textContent = q.question;
  // diagram questions: the box is hidden until answered, then the completed diagram is shown
  showVisual($("q-visual"), (check && check.answer_visual_html) || q.visual_html, "Lesson diagram");
  setError($("quiz-error"), "");

  $("options").replaceChildren(
    ...q.options.map((text, i) => {
      const b = el("button", "opt");
      b.type = "button";
      b.append(el("span", "key", KEYS[i]), el("span", "", text));
      if (picks[pos] === i) b.classList.add("picked");
      if (check) {
        b.disabled = true;
        if (i === check.answer_index) b.classList.add("right");
        else if (i === picks[pos]) b.classList.add("wrong");
      }
      b.addEventListener("click", () => choose(i));
      return b;
    })
  );

  const ex = $("explain");
  if (check) {
    ex.replaceChildren(el("strong", "", check.correct ? "Correct!" : "Not quite."), check.explanation);
    ex.classList.toggle("bad", !check.correct);
  }
  ex.classList.toggle("hidden", !check);

  $("next").textContent = pos === total - 1 ? "See results" : "Next →";
  $("next").disabled = !check; // no skipping
}

async function choose(i) {
  if (checks[pos] || expired) return;
  picks[pos] = i;
  for (const b of $("options").children) b.disabled = true;
  try {
    checks[pos] = await api(`/api/quiz/${quiz.id}/check`, { index: pos, choice: i });
  } catch (err) {
    picks[pos] = null;
    renderQuestion();
    setError($("quiz-error"), err.status === 404 ? "This quiz expired (the server restarted). Start a new topic." : err.message);
    if (err.status === 404) {
      expired = true;
      $("next").textContent = "New topic";
      $("next").disabled = false;
    }
    return;
  }
  renderQuestion();
}

$("next").addEventListener("click", () => {
  if (expired) { show("start"); return; }
  if (pos < quiz.questions.length - 1) { pos++; renderQuestion(); return; }
  renderResults();
});

document.addEventListener("keydown", (e) => {
  if ($("quiz").classList.contains("hidden") || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key.toUpperCase();
  let i = "1234".indexOf(key);
  if (i < 0) i = KEYS.indexOf(key);
  if (i >= 0 && i < quiz.questions[pos].options.length) { e.preventDefault(); choose(i); }
  else if (e.key === "Enter" && !$("next").disabled) { e.preventDefault(); $("next").click(); }
});

// ---------------------------------------------------------------- results
function renderResults() {
  const score = checks.filter((c) => c.correct).length;
  const total = checks.length;
  $("score").textContent = `${score} / ${total} - ${Math.round((score / total) * 100)}%`;
  $("results-title").textContent = quiz.title;
  setError($("results-error"), "");
  // missed anything? offer the two ways to fix it; a clean follow-up gets a pat on the back instead
  const missed = total - score;
  $("weak-actions").classList.toggle("hidden", !missed);
  $("results-note").textContent = quiz.focus.length && !missed ? "Nice: those weak spots look fixed." : "";
  $("results-note").classList.toggle("hidden", !$("results-note").textContent);
  $("review").replaceChildren(
    ...checks.map((c, i) => {
      const q = quiz.questions[i];
      const li = el("li");
      li.append(el("div", "q", `${i + 1}. ${q.question}`));
      li.append(el("div", `a ${c.correct ? "ok" : "no"}`, `${c.correct ? "✓" : "✗"} Your answer: ${q.options[picks[i]]}`));
      if (!c.correct) li.append(el("div", "a ok", `Correct answer: ${q.options[c.answer_index]}`));
      li.append(el("div", "why", c.explanation));
      return li;
    })
  );
  show("results");
}

$("back-to-lesson").addEventListener("click", () => show("lesson"));
$("restart").addEventListener("click", () => { show("start"); $("topic").focus(); });

// What the learner got wrong -> (a) flashcards to memorise it, (b) a new quiz on the same ideas with
// different questions. The server remembers which answers were wrong, so the page only asks.
function expiredOr(err, what) {
  return err.status === 404 ? `This ${what} expired (the server restarted). Start a new topic.` : err.message;
}

$("followup").addEventListener("click", () => {
  setError($("results-error"), "");
  busy($("followup"), "Writing new questions… (≈40 s)", async () => {
    try {
      beginQuiz(await api(`/api/quiz/${quiz.id}/followup`, {}));
    } catch (err) {
      setError($("results-error"), expiredOr(err, "quiz"));
    }
  });
});

$("mistake-cards").addEventListener("click", () => {
  setError($("results-error"), "");
  busy($("mistake-cards"), "Making cards…", async () => {
    try {
      openDeck(await api(`/api/quiz/${quiz.id}/flashcards`, { which: "missed" }), "results", quiz.id);
    } catch (err) {
      setError($("results-error"), expiredOr(err, "quiz"));
    }
  });
});

// ---------------------------------------------------------------- 2b. flashcards
// Flip, then "Still learning" (card comes back in a few) or "Got it" (retired).
// The server keeps the review queue and decides which card is next.
$("make-cards").addEventListener("click", () => {
  setError($("lesson-error"), "");
  busy($("make-cards"), "Writing cards…", async () => {
    try {
      openDeck(await api(`/api/lessons/${lesson.id}/flashcards`, { num_cards: 8 }), "lesson", null);
    } catch (err) {
      setError($("lesson-error"), err.status === 404 ? "This lesson expired (the server restarted). Start a new topic." : err.message);
    }
  });
});

// Open a deck. `back` is the screen the Back button returns to; `fromQuiz` is set for decks made from
// a quiz's mistakes, which offer a follow-up quiz once the cards are learned.
function openDeck(d, back, fromQuiz) {
  deck = d;
  cardState = deck.state;
  cardsBack = back;
  deckQuizId = fromQuiz;
  flipped = false;
  renderCard();
  show("cards");
}

function renderCard() {
  const done = cardState.next === null;
  $("card-progress").textContent = `${deck.title}`;
  $("card-known").textContent = `${cardState.known} / ${cardState.total} learned`;
  $("card-bar").style.width = `${(cardState.known / cardState.total) * 100}%`;
  $("card-face").classList.toggle("hidden", done);
  $("card-done").classList.toggle("hidden", !done);
  $("card-flip").classList.toggle("hidden", done || flipped);
  $("card-again").classList.toggle("hidden", done || !flipped);
  $("card-know").classList.toggle("hidden", done || !flipped);
  setError($("card-error"), "");
  if (done) return renderDeckDone();

  const card = deck.cards[cardState.next];
  $("card-face").classList.toggle("back", flipped);
  $("card-side").textContent = flipped ? "Answer" : card.kind === "diagram" ? "Diagram card" : "Question";
  $("card-text").textContent = flipped ? card.back : card.front;
  showVisual($("card-visual"), flipped ? card.back_visual_html : card.front_visual_html, "Lesson diagram");
}

function renderDeckDone() {
  showVisual($("card-visual"), null);
  $("card-done-title").textContent = `All ${cardState.total} cards learned in ${cardState.reviews} reviews`;
  $("card-hard").replaceChildren(
    ...cardState.hard.map((i) => {
      const li = el("li");
      li.append(el("div", "q", deck.cards[i].front), el("div", "why", deck.cards[i].back));
      return li;
    })
  );
  if (cardState.hard.length) $("card-hard").prepend(el("li", "q", "Cards that took a few tries:"));
  $("card-followup").classList.toggle("hidden", !deckQuizId);
}

async function reviewCard(result) {
  if (!flipped || cardState.next === null) return;
  try {
    cardState = await api(`/api/flashcards/${deck.id}/review`, { card: cardState.next, result });
  } catch (err) {
    setError($("card-error"), err.status === 404 ? "This deck expired (the server restarted). Go back and make a new one." : err.message);
    return;
  }
  flipped = false;
  renderCard();
}

function flipCard() {
  if (cardState.next === null || flipped) return;
  flipped = true;
  renderCard();
}

$("card-flip").addEventListener("click", flipCard);
$("card-again").addEventListener("click", () => reviewCard("again"));
$("card-know").addEventListener("click", () => reviewCard("know"));
$("cards-back").addEventListener("click", () => { flipped = false; show(cardsBack); });
$("card-followup").addEventListener("click", () => {
  setError($("card-error"), "");
  busy($("card-followup"), "Writing new questions… (≈40 s)", async () => {
    try {
      beginQuiz(await api(`/api/quiz/${deckQuizId}/followup`, {}));
    } catch (err) {
      setError($("card-error"), expiredOr(err, "quiz"));
    }
  });
});

document.addEventListener("keydown", (e) => {
  if ($("cards").classList.contains("hidden") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); flipCard(); }
  else if (e.key === "1") reviewCard("again");
  else if (e.key === "2") reviewCard("know");
});
