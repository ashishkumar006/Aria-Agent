import { Presentation, PresentationFile } from "@oai/artifact-tool";
import { writeFile, mkdir } from "node:fs/promises";

const OUT = "C:/Users/AISHWARYA/Downloads/project3/build_llm_deck";

const ACCENT = "indigo-600";
const ACCENT_SOFT = "indigo-100";
const DARK = "slate-950";
const BODY = "slate-600";
const MUTE = "slate-400";

const presentation = Presentation.create({ slideSize: { width: 1280, height: 720 } });

function addText(slide, opts) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    name: opts.name,
    position: { left: opts.left, top: opts.top, width: opts.width, height: opts.height },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = opts.text;
  shape.text.style = {
    fontSize: opts.fontSize ?? 18,
    bold: opts.bold ?? false,
    color: opts.color ?? BODY,
    align: opts.align ?? "left",
    leading: opts.leading ?? 1.25,
  };
  return shape;
}

function chip(slide, name, left, top, width, height, text, fill, textColor) {
  const box = slide.shapes.add({
    geometry: "roundRect",
    name,
    position: { left, top, width, height },
    fill,
    line: { style: "solid", fill: "none", width: 0 },
    borderRadius: "rounded-xl",
  });
  const t = addText(slide, {
    name: name + "-t",
    left: left,
    top: top,
    width: width,
    height: height,
    text,
    fontSize: 16,
    bold: true,
    color: textColor,
    align: "center",
  });
  return { box, t };
}

function titleSlide(slide, eyebrow, title, subtitle) {
  slide.background.fill = "slate-50";
  addText(slide, { name: "bar", left: 72, top: 64, width: 96, height: 10, text: "", });
  const bar = slide.shapes.add({
    geometry: "roundRect",
    name: "accentbar",
    position: { left: 72, top: 64, width: 64, height: 8 },
    fill: ACCENT,
    line: { style: "solid", fill: "none", width: 0 },
    borderRadius: "rounded",
  });
  addText(slide, { name: "eyebrow", left: 72, top: 96, width: 1136, height: 28, text: eyebrow, fontSize: 16, bold: true, color: ACCENT });
  addText(slide, { name: "title", left: 72, top: 132, width: 1136, height: 120, text: title, fontSize: 46, bold: true, color: DARK, leading: 1.1 });
  if (subtitle) {
    addText(slide, { name: "subtitle", left: 72, top: 270, width: 1000, height: 80, text: subtitle, fontSize: 20, color: BODY, leading: 1.4 });
  }
}

// Page footer
function footer(slide, n) {
  addText(slide, { name: "foot", left: 72, top: 684, width: 600, height: 24, text: "How Large Language Models Work", fontSize: 12, color: MUTE });
  addText(slide, { name: "pagenum", left: 1180, top: 684, width: 28, height: 24, text: String(n), fontSize: 12, color: MUTE, align: "right" });
}

// ---------- Slide 1: Title ----------
{
  const slide = presentation.slides.add();
  slide.background.fill = ACCENT;
  addText(slide, { name: "kicker", left: 96, top: 200, width: 1088, height: 30, text: "A FRIENDLY GUIDE", fontSize: 18, bold: true, color: "indigo-100" });
  addText(slide, { name: "title", left: 96, top: 240, width: 1088, height: 160, text: "How Large Language Models Work", fontSize: 60, bold: true, color: "white", leading: 1.05 });
  addText(slide, { name: "sub", left: 96, top: 430, width: 900, height: 60, text: "From raw text to fluent language — the ideas behind the models powering today's AI.", fontSize: 22, color: "indigo-100", leading: 1.4 });
}

// ---------- Slide 2: What is an LLM ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "THE BIG PICTURE", "An LLM is a next-word prediction machine", "Trained on vast text, it learns patterns of language so well that it can continue any prompt with plausible, useful text.");
  chip(slide, "c1", 96, 400, 320, 160, "Reads a prompt\n(\"The capital of France is\")", "white", DARK);
  const a1 = slide.shapes.add({ geometry: "rightArrow", name: "a1", position: { left: 432, top: 472, width: 90, height: 16 }, fill: ACCENT, line: { style: "solid", fill: "none", width: 0 } });
  chip(slide, "c2", 548, 400, 320, 160, "Predicts the most likely next token", "white", DARK);
  const a2 = slide.shapes.add({ geometry: "rightArrow", name: "a2", position: { left: 884, top: 472, width: 90, height: 16 }, fill: ACCENT, line: { style: "solid", fill: "none", width: 0 } });
  chip(slide, "c3", 1000, 400, 184, 160, "Repeats,\nbuilding a reply", ACCENT_SOFT, DARK);
  footer(slide, 2);
}

// ---------- Slide 3: Tokens ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "UNIT OF LANGUAGE", "Models think in tokens, not letters", "Text is split into tokens — word pieces, characters, or syllables. The model processes and predicts tokens, not raw characters.");
  addText(slide, { name: "src", left: 96, top: 380, width: 1088, height: 30, text: "Input:  \"Hello, world!\"", fontSize: 22, bold: true, color: DARK });
  const tokens = ["Hello", ",", " world", "!", " (3 tokens in GPT-4)"];
  let x = 96;
  let i = 0;
  for (const tk of tokens) {
    const w = tk.length > 10 ? 260 : 150;
    chip(slide, "tk" + i, x, 430, w, 64, tk, i === tokens.length - 1 ? ACCENT_SOFT : "white", i === tokens.length - 1 ? DARK : ACCENT);
    x += w + 16;
    i++;
  }
  addText(slide, { name: "note", left: 96, top: 540, width: 1088, height: 60, text: "One token ≈ 4 characters of English on average. Longer prompts and answers simply mean more tokens to process.", fontSize: 18, color: BODY, leading: 1.4 });
  footer(slide, 3);
}

// ---------- Slide 4: Training ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "LEARNING", "Pre-training learns by filling in the blanks", "Feed the model huge text, hide some words, and train it to predict them. Over billions of examples it absorbs grammar, facts, and reasoning patterns.");
  const items = [
    ["1. Massive text", "Books, articles, code, and web text"],
    ["2. Mask / shift", "Hide a token; ask the model to predict it"],
    ["3. Measure error", "Compare guess to the real token"],
    ["4. Adjust weights", "Tune the network to reduce mistakes"],
  ];
  let y = 400;
  for (const it of items) {
    chip(slide, "b" + it[0], 96, y, 220, 130, it[0], ACCENT_SOFT, DARK);
    addText(slide, { name: "bt" + it[0], left: 336, top: y + 24, width: 820, height: 90, text: it[1], fontSize: 20, color: BODY, leading: 1.3 });
    y += 150;
  }
  footer(slide, 4);
}

// ---------- Slide 5: Transformer & attention ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "THE ENGINE", "Transformers read everything at once with attention", "The breakthrough is self-attention: every token looks at every other token to decide what matters, capturing long-range meaning in parallel.");
  chip(slide, "p", 96, 410, 360, 170, "Input tokens\nenter the model together", "white", DARK);
  chip(slide, "att", 520, 410, 280, 170, "Self-attention\nweights each token's\nrelation to the others", ACCENT, "white");
  chip(slide, "out", 888, 410, 296, 170, "Rich context-aware\nrepresentations", "white", DARK);
  const a1 = slide.shapes.add({ geometry: "rightArrow", name: "ar1", position: { left: 464, top: 488, width: 48, height: 16 }, fill: ACCENT, line: { style: "solid", fill: "none", width: 0 } });
  const a2 = slide.shapes.add({ geometry: "rightArrow", name: "ar2", position: { left: 832, top: 488, width: 48, height: 16 }, fill: ACCENT, line: { style: "solid", fill: "none", width: 0 } });
  addText(slide, { name: "cap", left: 96, top: 600, width: 1088, height: 40, text: "Stacked attention layers build deeper understanding, which is why modern LLMs are built from many transformer blocks.", fontSize: 17, color: BODY, align: "center" });
  footer(slide, 5);
}

// ---------- Slide 6: Scale ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "WHY BIG", "Scale is a quiet superpower", "More parameters and more data unlock surprising abilities. Capabilities often improve smoothly as models grow — a property called emergent behavior.");
  chip(slide, "s1", 96, 410, 340, 180, "Parameters\n\nBillions of knobs the model tunes while learning", ACCENT_SOFT, DARK);
  chip(slide, "s2", 470, 410, 340, 180, "Training data\n\nTrillions of tokens of text and code", "white", DARK);
  chip(slide, "s3", 844, 410, 340, 180, "Compute\n\nMassive GPU time to train the weights", "white", DARK);
  footer(slide, 6);
}

// ---------- Slide 7: Fine-tuning & RLHF ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "POLISHING", "Fine-tuning teaches helpful, safer behavior", "After pre-training, models are aligned using human feedback (RLHF): people rank answers, and the model learns to prefer the most useful, harmless responses.");
  const steps = [
    ["Base model", "Predicts text, but not yet steerable"],
    ["Human / AI feedback", "Rank which answers are best"],
    ["Reward model", "Learns your preferences"],
    ["Aligned assistant", "Gives helpful, safer replies"],
  ];
  let x = 96;
  for (let i = 0; i < steps.length; i++) {
    const w = 250;
    chip(slide, "f" + i, x, 420, w, 150, steps[i][0], i === 3 ? ACCENT : "white", i === 3 ? "white" : DARK);
    addText(slide, { name: "ft" + i, left: x, top: 580, width: w, height: 70, text: steps[i][1], fontSize: 15, color: BODY, align: "center", leading: 1.2 });
    if (i < steps.length - 1) {
      slide.shapes.add({ geometry: "rightArrow", name: "fa" + i, position: { left: x + w + 4, top: 483, width: 30, height: 14 }, fill: ACCENT, line: { style: "solid", fill: "none", width: 0 } });
    }
    x += w + 36;
  }
  footer(slide, 7);
}

// ---------- Slide 8: Inference ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "GENERATING TEXT", "Inference produces one token at a time", "At reply time the model guesses the next token, appends it, and repeats — sampling among likely options so answers vary while staying on topic.");
  addText(slide, { name: "seqlbl", left: 96, top: 380, width: 1088, height: 30, text: "Prompt → token → append → repeat", fontSize: 22, bold: true, color: DARK });
  const seq = ["The", "cat", "sat", "on"];
  let x = 96;
  for (let i = 0; i < seq.length; i++) {
    chip(slide, "sq" + i, x, 430, 130, 70, seq[i], i === seq.length - 1 ? ACCENT : "white", i === seq.length - 1 ? "white" : ACCENT);
    if (i < seq.length - 1) {
      slide.shapes.add({ geometry: "rightArrow", name: "sqa" + i, position: { left: x + 130 + 2, top: 458, width: 26, height: 12 }, fill: MUTE, line: { style: "solid", fill: "none", width: 0 } });
    }
    x += 130 + 28;
  }
  addText(slide, { name: "next", left: 96, top: 540, width: 1088, height: 40, text: "Next predicted token: \" the \"  →  then \" mat \"  →  ... until a stop signal ends the reply.", fontSize: 18, color: BODY, leading: 1.4 });
  footer(slide, 8);
}

// ---------- Slide 9: Context window ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "MEMORY", "The context window is its working memory", "A model only 'sees' the tokens in its context window (prompt + reply). Anything beyond it is forgotten, which is why long chats summarize or lose earlier details.");
  // window bar
  slide.shapes.add({ geometry: "roundRect", name: "win", position: { left: 96, top: 410, width: 1088, height: 90 }, fill: ACCENT_SOFT, line: { style: "solid", fill: "none", width: 0 }, borderRadius: "rounded-2xl" });
  addText(slide, { name: "wint", left: 96, top: 432, width: 1088, height: 50, text: "Context window  =  system + prompt + conversation + reply   (e.g. 8K–200K+ tokens)", fontSize: 18, bold: true, color: DARK, align: "center" });
  addText(slide, { name: "cap", left: 96, top: 530, width: 1088, height: 60, text: "More tokens cost more compute, so longer context is powerful but not free. Retrieval and summarization help stretch what fits.", fontSize: 17, color: BODY, leading: 1.4 });
  footer(slide, 9);
}

// ---------- Slide 10: Strengths & limits ----------
{
  const slide = presentation.slides.add();
  titleSlide(slide, "BALANCED VIEW", "Impressive, but not a source of truth", "LLMs are fluent pattern engines. They reason well on familiar patterns yet can confidently state things that are wrong.");
  chip(slide, "good", 96, 400, 520, 230, "Strengths\n\n• Summarize, draft, and explain\n• Translate and code across languages\n• Adapt to many tasks from one prompt", "white", DARK);
  chip(slide, "lim", 664, 400, 520, 230, "Limitations\n\n• No real-world experience or live facts\n• Can hallucinate or guess\n• Biased by training data; no true understanding", "white", DARK);
  footer(slide, 10);
}

// ---------- Slide 11: Takeaways ----------
{
  const slide = presentation.slides.add();
  slide.background.fill = ACCENT;
  addText(slide, { name: "k", left: 96, top: 150, width: 1088, height: 30, text: "TAKEAWAYS", fontSize: 18, bold: true, color: "indigo-100" });
  addText(slide, { name: "t", left: 96, top: 190, width: 1088, height: 80, text: "What to remember", fontSize: 48, bold: true, color: "white" });
  addText(slide, { name: "pts", left: 96, top: 300, width: 1088, height: 300, text:
    "• An LLM predicts the next token from what came before.\n• It works in tokens, learned through large-scale pre-training.\n• Transformers + attention let it understand context at scale.\n• Fine-tuning aligns it to be helpful and safer.\n• It's a powerful language engine — verify its facts.", fontSize: 24, color: "indigo-100", leading: 1.5 });
}

const pptx = await PresentationFile.exportPptx(presentation);
await mkdir(OUT, { recursive: true });
await pptx.save(OUT + "/llm-explained.pptx");
console.log("Saved deck ->", OUT + "/llm-explained.pptx");
