import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * The Code panel's model picker.
 *
 * Two failure modes this pins, both of which look fine until they cost money:
 *
 *  - a pick that never reaches the gateway. `api.codeAskStream` is a
 *    hand-rolled fetch, not the `j()` funnel, so nothing forces it to send
 *    `model`; the panel would look like it works and quietly keep answering
 *    from the gateway default.
 *  - a pick that reverts on reload. Persisting the model matters more than
 *    persisting cosmetics here: silently reverting means paying for a frontier
 *    model (or, worse, getting a free one) that the user did not choose.
 *
 * Source-level rather than behavioural, matching AgentPanel.persistence.test.ts.
 */
const PANEL = readFileSync(resolve(__dirname, 'ModelPicker.tsx'), 'utf-8');
const API = readFileSync(resolve(__dirname, '..', 'api.ts'), 'utf-8');
const CODE = readFileSync(resolve(__dirname, '..', 'views', 'Code.tsx'), 'utf-8');
const CHAT = readFileSync(resolve(__dirname, '..', 'views', 'Chat.tsx'), 'utf-8');

describe('model picker', () => {
  it('exposes a roster endpoint through the agent proxy', () => {
    // Direct gateway calls are blocked twice over: no CORS middleware on
    // :8109, and the page never receives the gateway token.
    expect(API).toMatch(/models:\s*\(refresh = false.*\)\s*=>/);
    expect(API).toMatch(/\/api\/models/);
    expect(API).not.toMatch(/gatewayPort[^)]*\)\s*\/v1\/providers/);
  });

  it('forwards the chosen model on every ask', () => {
    expect(API).toMatch(/model\?: string\): AsyncGenerator<CodeAskFrame>/);
    // Conditional spread, not `model: model`: an empty pick must leave the
    // request identical to the pre-picker shape so the gateway default still
    // applies rather than receiving `model: ""`.
    expect(API).toMatch(/\.\.\.\(model \? \{ model \} : \{\}\)/);
  });

  it('threads the pick from Code.tsx into the panel and the request', () => {
    expect(CODE).toMatch(/model=\{aiModel\}/);
    expect(CODE).toMatch(/onModelChange=\{setAiModel\}/);
    // The panel sends the pick; Code.tsx supplies it to the stream call.
    expect(CODE).toMatch(/aiModel \|\| undefined/);
  });

  it('persists the pick so a reload cannot change the bill', () => {
    expect(CODE).toMatch(/localStorage\.getItem\('aria\.code\.model'\)/);
    expect(CODE).toMatch(/localStorage\.setItem\('aria\.code\.model', aiModel\)/);
  });

  it('keeps "unset" distinct from "set to empty"', () => {
    expect(CODE).toMatch(/else localStorage\.removeItem\('aria\.code\.model'\)/);
  });

  it('offers the whole catalogue, with free models grouped first', () => {
    // An <optgroup> is the only native way to actually band a <select>; a text
    // prefix on every option reads as noise and does not separate anything.
    expect(PANEL).toMatch(/<optgroup label=\{`Free — \$\{free\.length\} models`\}>/);
    expect(PANEL).toMatch(/<optgroup label=\{`Paid — \$\{paid\.length\} total`\}>/);
    // Free must be rendered before paid, not merely present.
    expect(PANEL.indexOf('free.map')).toBeLessThan(PANEL.indexOf('shownPaid.map'));
    expect(PANEL).toMatch(/const free = all\.filter\(\(m\) => m\.is_free\)/);
    expect(PANEL).toMatch(/const paid = all\.filter\(\(m\) => !m\.is_free\)/);
    // all=true, or a paid key's models would be unreachable from the UI.
    // The call site is in the panel; the signature is in api.ts.
    expect(PANEL).toMatch(/api\.models\(refresh, true\)/);
    expect(API).toMatch(/models: \(refresh = false, all = false/);
    expect(API).toMatch(/refresh && 'refresh=true', all && 'all=true'/);
  });

  it('caps the paid group but keeps every free model visible', () => {
    // Free models are the ones actually used here; none may be hidden.
    // Paid is capped so the list stays navigable, with an explicit opt-in.
    expect(PANEL).toMatch(/const shownPaid = expanded \? paid : paid\.slice\(0, 60\)/);
    expect(PANEL).toMatch(/\{free\.map\(\(m\) => \(/);
    expect(PANEL).toMatch(/\{paid\.length > shownPaid\.length && \(/);
  });

  it('offers Auto plus the roster, with EVERY model cost-flagged', () => {
    expect(PANEL).toMatch(/<option value="">Auto \(gateway default\)<\/option>/);
    // Both halves carry a cost marker. It used to be FREE-only, which read as
    // "the paid ones are free" rather than the reverse - and a bare id gave no
    // way to tell two same-vendor models apart or see the context window.
    expect(PANEL).toMatch(/const cost_of = \(m: KiloModel\) => \(m\.is_free \? 'FREE' : 'PAID'\)/);
    expect(PANEL).toMatch(/const pretty = \(m: KiloModel\)/);
    expect(PANEL).toMatch(/ctx/);
    // The readable name is for humans; the VALUE is still the bare id, so
    // nothing about routing changes.
    expect(PANEL).toMatch(/<option key=\{m\.id\} value=\{m\.id\} title=\{title_of\(m\)\}>\{label_of\(m\)\}<\/option>/);
  });

  it('disables the control when the roster is empty', () => {
    expect(PANEL).toMatch(/disabled=\{busy \|\| !onChange \|\| all\.length === 0\}/);
  });

  it('surfaces the model that actually answered, not the one requested', () => {
    // With "Auto" the gateway chooses, so echoing the request would be a lie.
    // The step is emitted from Code.tsx's agent loop, which owns the frames.
    expect(CODE).toMatch(/if \(f\.model \|\| f\.provider\)/);
    expect(CODE).toMatch(/label: 'model'/);
  });

  it('does not pin the code default onto Chat', () => {
    // The roster default is the *code* model. Chat must stay on "Auto" (the
    // gateway routes for agent="chat") unless the user picks one, or every chat
    // silently runs on a code model.
    expect(PANEL).toMatch(/applyDefault = true/);
    expect(PANEL).toMatch(/if \(!applyDefault\) return;/);
    expect(CHAT).toMatch(/applyDefault=\{false\}/);
  });

  it('lets Chat choose the engine and a model, pinned per thread', () => {
    expect(CHAT).toMatch(/type ChatMode = 'light' \| 'dag'/);
    expect(CHAT).toMatch(/aria\.chat\.mode/);
    expect(CHAT).toMatch(/aria\.chat\.prefs/);
    expect(CHAT).toMatch(/api\.chatSimpleStream\(q, cid, ac\.signal, useModel \|\| undefined\)/);
    expect(CHAT).toMatch(/api\.chat\(q, cid, ac\.signal, \{ runKind: 'chat' \}\)/);
  });
});