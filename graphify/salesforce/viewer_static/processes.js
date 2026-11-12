"use strict";

const get = (id) => document.getElementById(id);
const menus = {country: get("country-select"), feature: get("feature-select"), process: get("process-select")};
const stepKinds = {start: "Start", action: "Action", decision: "Decision", outcome: "Outcome"};
const effectKinds = {
  read: "Read",
  assign: "Assign in memory",
  create: "Create records",
  update: "Update records",
  upsert: "Upsert records",
  delete: "Delete records",
  call: "Call",
  condition: "Condition",
};
const writeEffects = new Set(["create", "update", "upsert", "delete"]);
const allFeatures = {id: "", name: "All features"};
const componentGroups = [
  {label: "Objects", kinds: ["object"]},
  {label: "Apex and Flows", kinds: ["apexclass", "apexmethod", "apextrigger", "flow"]},
  {label: "Fields", kinds: ["field"]},
  {label: "Other components", kinds: []},
];
const narrowLayout = matchMedia("(max-width: 850px)");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");
const state = {catalog: null, country: null, feature: null, process: null, diagram: null, step: null, revealAt: ""};

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function svgElement(tag, attributes) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function routeTo(values) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value) params.set(key, value);
  return "#" + params.toString();
}

function setOptions(menu, items, selected, labelKey) {
  const options = items.map((item) => new Option(item[labelKey], item.id));
  menu.replaceChildren(...options);
  menu.value = selected?.id || "";
  menu.disabled = !options.length;
}

function selectItem(items, id) {
  return items.find((item) => item.id === id) || items[0] || null;
}

function revealDetail() {
  get("step-title").focus({preventScroll: true});
  get("process-detail").scrollIntoView({behavior: reduceMotion.matches ? "auto" : "smooth", block: "start"});
}

function stepLink(step, label, className) {
  const link = element("a", label, className);
  link.href = routeTo({
    country: state.country.id,
    feature: state.feature.id,
    process: state.process.id,
    step: step.id,
  });
  link.addEventListener("click", (event) => {
    if (!narrowLayout.matches || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (location.hash === link.hash) revealDetail();
    else state.revealAt = link.hash;
  });
  return link;
}

function componentLink(component) {
  const link = element("a", component.api_name, "component-link");
  link.href = "/explorer#" + encodeURIComponent(component.id);
  return link;
}

function componentLinks(components) {
  const content = document.createDocumentFragment();
  const byGroup = new Map(componentGroups.map((group) => [group, []]));
  for (const component of components) {
    const group = componentGroups.find(({kinds}) => kinds.includes(component.kind)) || componentGroups.at(-1);
    byGroup.get(group).push(component);
  }
  for (const [group, members] of byGroup) {
    if (!members.length) continue;
    const section = element("div", "", "component-group");
    const links = element("div", "", "component-links");
    links.append(...members.map(componentLink));
    section.append(element("h5", group.label), links);
    content.append(section);
  }
  return content;
}

function diagramLayers(steps, connections) {
  const queue = steps.filter((step) => connections.get(step.id).incoming.length === 0);
  const remaining = new Map(steps.map((step) => [step.id, connections.get(step.id).incoming.length]));
  const ranks = new Map(queue.map((step) => [step.id, 0]));
  for (const step of queue) {
    for (const edge of connections.get(step.id).outgoing) {
      ranks.set(edge.to, Math.max(ranks.get(edge.to) || 0, ranks.get(step.id) + 1));
      remaining.set(edge.to, remaining.get(edge.to) - 1);
      if (remaining.get(edge.to) === 0) queue.push(connections.get(edge.to).step);
    }
  }
  let nextRank = Math.max(-1, ...ranks.values()) + 1;
  const layers = [];
  for (const step of steps) {
    const rank = remaining.get(step.id) === 0 ? ranks.get(step.id) : nextRank++;
    (layers[rank] ||= []).push(step);
  }
  return layers.filter(Boolean);
}

function makeDiagram(process) {
  const connections = new Map(process.steps.map((step) => [step.id, {step, incoming: [], outgoing: []}]));
  const components = new Map();
  for (const step of process.steps) {
    for (const component of step.components) components.set(component.id, component);
  }
  for (const edge of process.edges) {
    connections.get(edge.from).outgoing.push(edge);
    connections.get(edge.to).incoming.push(edge);
  }

  const nodes = new Map();
  const layers = diagramLayers(process.steps, connections);
  const rows = layers.map((steps) => {
    const row = element("div", "", "process-layer");
    for (const step of steps) {
      const link = stepLink(step, "", "process-step");
      if (Object.hasOwn(stepKinds, step.kind)) link.classList.add(step.kind);
      link.append(element("span", stepKinds[step.kind] || "Step", "meta-label"), element("strong", step.label));
      const branches = [...new Set(connections.get(step.id).incoming.map((edge) => edge.label).filter(Boolean))];
      if (branches.length) link.append(element("span", branches.join(" · "), "process-branch"));
      const preferred = step.components.filter((component) => ["object", "apexclass", "flow", "apextrigger"].includes(component.kind));
      const preview = preferred.length ? preferred : step.components;
      if (preview.length) {
        const labels = element("span", "", "process-step-components");
        for (const component of preview.slice(0, 3)) labels.append(element("span", component.api_name));
        if (preview.length > 3) labels.append(element("span", "+" + (preview.length - 3)));
        link.append(labels);
      }
      nodes.set(step.id, link);
      row.append(link);
    }
    return row;
  });
  get("process-layers").replaceChildren(...rows);
  get("process-components").replaceChildren(componentLinks(components.values()));
  get("process-participants").hidden = !components.size;
  get("process-participants").open = false;
  return {connections, components, nodes, edges: process.edges};
}

function arrowMarker(id, active) {
  const marker = svgElement("marker", {
    id, markerWidth: 7, markerHeight: 7, refX: 6, refY: 3.5, orient: "auto", markerUnits: "userSpaceOnUse",
  });
  marker.append(svgElement("polygon", {points: "0,0 7,3.5 0,7", class: active ? "active" : ""}));
  return marker;
}

let arrowFrame;
function queueArrows() {
  if (arrowFrame) return;
  arrowFrame = requestAnimationFrame(() => {
    arrowFrame = null;
    drawArrows();
  });
}

function drawArrows() {
  if (!state.diagram || !state.step) return;
  const {nodes, edges} = state.diagram;
  const diagram = get("process-diagram").getBoundingClientRect();
  if (!diagram.width || !diagram.height) return;

  const svg = get("process-arrows");
  svg.setAttribute("viewBox", [0, 0, diagram.width, diagram.height].join(" "));
  const defs = svgElement("defs", {});
  defs.append(arrowMarker("process-arrow", false), arrowMarker("process-arrow-active", true));
  const bounds = new Map([...nodes].map(([id, node]) => [id, node.getBoundingClientRect()]));
  const paths = [];
  const activePaths = [];
  for (const edge of edges) {
    const from = bounds.get(edge.from);
    const to = bounds.get(edge.to);
    const x1 = (from.left + from.right) / 2 - diagram.left;
    const x2 = (to.left + to.right) / 2 - diagram.left;
    const y1 = from.bottom - diagram.top + 1;
    const y2 = to.top - diagram.top - 3;
    const sameLayer = Math.abs(from.top - to.top) < 4;
    const crossesLayer = y2 - y1 > 66;
    const lane = diagram.width - 8;
    const side = y1 >= y2 || crossesLayer;
    const middle = (y1 + y2) / 2;
    const points = side
      ? ["M", x1, y1, "V", y1 + 7, "H", lane, "V", y2 - 7, "H", x2, "V", y2]
      : ["M", x1, y1, "V", middle, "H", x2, "V", y2];
    const active = edge.from === state.step.id || edge.to === state.step.id;
    const path = svgElement("path", {
      d: points.join(" "),
      class: [active ? "active" : "", sameLayer || y1 >= y2 ? "loop" : ""].join(" "),
      "marker-end": active ? "url(#process-arrow-active)" : "url(#process-arrow)",
    });
    (active ? activePaths : paths).push(path);
  }
  svg.replaceChildren(defs, ...paths, ...activePaths);
}

function sourceList(sources) {
  const list = element("ul", "", "source-list");
  for (const source of sources) {
    const row = element("li", "", "source-row");
    const location = source.path + (source.line ? ":" + source.line : "");
    row.append(element("strong", source.label), element("code", location, "source-location"));
    if (source.snippet) row.append(element("pre", source.snippet, "source-snippet"));
    const matches = source.snapshot_matches || [];
    if (!matches.length) row.append(element("span", "Documented source", "hint"));
    for (const match of matches) {
      const link = element("a", "Snapshot evidence · " + match.rule, "snapshot-reference");
      link.href = "/api/evidence?" + new URLSearchParams({relationship: match.relationship});
      link.target = "_blank";
      link.rel = "noopener";
      row.append(link);
    }
    list.append(row);
  }
  return list;
}

function effectList(effects) {
  const list = element("ul", "", "effect-list");
  for (const effect of effects) {
    const row = element("li", "", "effect-row");
    const kind = element("span", effectKinds[effect.kind] || effect.kind, "effect-kind");
    if (writeEffects.has(effect.kind)) kind.classList.add("is-write");
    row.append(kind, element("p", effect.summary));
    const component = state.diagram.components.get(effect.component);
    if (component) row.append(componentLink(component));
    if (effect.evidence.length) {
      const detail = element("details", "", "process-evidence");
      detail.append(element("summary", "Source evidence"), sourceList(effect.evidence));
      row.append(detail);
    }
    list.append(row);
  }
  return list;
}

function stepSection(title, content) {
  const section = element("section", "", "step-section");
  section.append(element("h4", title), content);
  return section;
}

function renderStep(step) {
  state.step = step;
  const {connections, nodes} = state.diagram;
  const {incoming, outgoing} = connections.get(step.id);
  const neighbors = new Set([...incoming.map((edge) => edge.from), ...outgoing.map((edge) => edge.to)]);
  for (const [id, link] of nodes) {
    if (id === step.id) link.setAttribute("aria-current", "step");
    else link.removeAttribute("aria-current");
    link.classList.toggle("neighbor", neighbors.has(id) && id !== step.id);
  }
  get("step-kind").textContent = stepKinds[step.kind] || "Step";
  get("step-title").textContent = step.label;
  get("step-description").textContent = step.description;
  const body = get("step-body");
  body.replaceChildren();
  if (step.effects.length) body.append(stepSection("Effects", effectList(step.effects)));
  if (step.components.length) body.append(stepSection("Components in this step", componentLinks(step.components)));
  if (step.missing_components?.length) {
    const list = element("ul", "", "source-list");
    for (const id of step.missing_components) {
      const row = element("li", "", "source-row");
      row.append(element("code", id));
      list.append(row);
    }
    body.append(stepSection("Components missing from the snapshot", list));
  }
  if (outgoing.length) {
    const links = element("div", "", "step-links");
    for (const edge of outgoing) {
      const next = connections.get(edge.to).step;
      const text = edge.label ? edge.label + " → " + next.label : next.label + " →";
      links.append(stepLink(next, text, "step-link"));
    }
    body.append(stepSection("Next steps", links));
  }
  if (step.evidence.length) body.append(stepSection("Step evidence", sourceList(step.evidence)));
  get("process-detail").scrollTop = 0;
  queueArrows();
}

function render() {
  const route = new URLSearchParams(location.hash.slice(1));
  const countries = state.catalog.countries;
  const country = selectItem(countries, route.get("country"));
  const countryFeatures = country?.features || [];
  const features = countryFeatures.length > 1 ? [allFeatures, ...countryFeatures] : countryFeatures;
  const feature = selectItem(features, route.get("feature"));
  const processes = feature === allFeatures
    ? [...new Map(countryFeatures.flatMap(({processes}) => processes.map((item) => [item.id, item]))).values()]
        .sort((left, right) => left.title.localeCompare(right.title))
    : feature?.processes || [];
  const process = selectItem(processes, route.get("process"));
  setOptions(menus.country, countries, country, "name");
  setOptions(menus.feature, features, feature, "name");
  setOptions(menus.process, processes, process, "title");
  const changed = state.process !== process || state.country !== country || state.feature !== feature;
  Object.assign(state, {country, feature, process});
  get("process-empty").hidden = Boolean(process);
  get("process-map").hidden = !process;
  if (!process) {
    state.diagram = null;
    state.step = null;
    return;
  }
  if (changed) {
    get("process-title").textContent = process.title;
    get("process-summary").textContent = process.summary;
    const review = get("process-review");
    review.hidden = process.review_status !== "needs_review";
    review.textContent = process.catalog_error || "Some components in this process need review.";
    get("process-grid").hidden = Boolean(process.catalog_error);
    state.step = null;
    state.diagram = process.catalog_error ? null : makeDiagram(process);
  }
  const step = selectItem(process.steps, route.get("step") || (feature === allFeatures ? null : process.entry_step));
  if (step) renderStep(step);
  if (state.revealAt && state.revealAt === location.hash) revealDetail();
  state.revealAt = "";
}

for (const [key, menu] of Object.entries(menus)) {
  menu.addEventListener("change", () => {
    const values = {};
    for (const [part, select] of Object.entries(menus)) {
      values[part] = select.value;
      if (part === key) break;
    }
    location.hash = routeTo(values);
  });
}
get("back-to-step").addEventListener("click", () => {
  const link = state.diagram?.nodes.get(state.step?.id);
  if (link) {
    link.focus({preventScroll: true});
    link.scrollIntoView({behavior: reduceMotion.matches ? "auto" : "smooth", block: "center"});
  }
});
window.addEventListener("hashchange", () => { if (state.catalog) render(); });
new ResizeObserver(queueArrows).observe(get("process-diagram"));

async function loadCatalog() {
  const message = get("process-message");
  try {
    const response = await fetch("/api/processes", {cache: "no-store", credentials: "same-origin"});
    const catalog = await response.json();
    if (!response.ok) throw new Error(catalog.error || "Process maps could not be loaded.");
    state.catalog = catalog;
    const revision = get("process-revision");
    revision.textContent = catalog.source_revision ? catalog.source_revision.slice(0, 12) : "Unavailable";
    revision.title = catalog.source_revision || "";
    message.hidden = true;
    get("process-content").hidden = false;
    render();
  } catch (error) {
    get("process-content").hidden = true;
    message.hidden = false;
    message.classList.add("error");
    message.textContent = error.message;
  }
}

loadCatalog();
