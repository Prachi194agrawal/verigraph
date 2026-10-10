"use client";

import cytoscape, { type ElementDefinition } from "cytoscape";
import { useEffect, useRef } from "react";
import type { GraphEdge, GraphNode } from "@/lib/types";

function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

/** Hop distance of every node from the focus (undirected), so the layout can ring nodes by distance. */
function distances(focus: string, edges: GraphEdge[]): Map<string, number> {
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    adj.set(e.source, [...(adj.get(e.source) ?? []), e.target]);
    adj.set(e.target, [...(adj.get(e.target) ?? []), e.source]);
  }
  const dist = new Map([[focus, 0]]);
  const queue = [focus];
  while (queue.length) {
    const n = queue.shift() as string;
    for (const m of adj.get(n) ?? []) {
      if (!dist.has(m)) {
        dist.set(m, (dist.get(n) ?? 0) + 1);
        queue.push(m);
      }
    }
  }
  return dist;
}

/** Cytoscape call-graph view: functions, classes and tests, with CALLS/INHERITS/TESTS/DEFINES edges.
 * Name-matched CALLS edges are drawn dashed: they are guesses and never feed gold answers. */
export default function CallGraph({ nodes, edges, focus, onSelect }: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  focus: string;
  onSelect: (id: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const select = useRef(onSelect);
  select.current = onSelect;

  useEffect(() => {
    if (!ref.current) return;
    const dist = distances(focus, edges);
    const ids = new Set(nodes.map((n) => n.id));
    const calls = new Set(edges.filter((e) => e.type === "CALLS").map((e) => `${e.source}>${e.target}`));
    // A TESTS edge always duplicates a CALLS edge from the same test: draw one line per pair.
    const shown = edges.filter((e) => ids.has(e.source) && ids.has(e.target)
      && !(e.type === "TESTS" && calls.has(`${e.source}>${e.target}`)));
    const busy = nodes.length > 20; // in busy neighborhoods, test labels appear on hover only
    const elements: ElementDefinition[] = [
      ...nodes.map((n) => ({
        data: {
          id: n.id, name: n.name, kind: n.is_test ? "Test" : n.kind,
          label: busy && n.is_test && n.id !== focus ? "" : n.name,
          level: 10 - Math.min(dist.get(n.id) ?? 3, 9) - (n.is_test ? 0.5 : 0), focus: n.id === focus ? 1 : 0,
        },
      })),
      ...shown.map((e, i) => ({
        data: { id: `e${i}`, source: e.source, target: e.target, type: e.type,
                guess: e.resolved_by === "name" ? 1 : 0, label: e.type === "INHERITS" ? "inherits" : "" },
      })),
    ];
    const ink = token("--text-primary", "#0b0b0b");
    const secondary = token("--text-secondary", "#52514e");
    const edge = token("--baseline", "#c3c2b7");
    const surface = token("--surface-1", "#fcfcfb");
    const cy = cytoscape({
      container: ref.current,
      elements,
      wheelSensitivity: 0.25,
      layout: { name: "concentric", concentric: (n: any) => n.data("level"), levelWidth: () => 0.5,
                minNodeSpacing: 34, animate: false },
      style: [
        { selector: "node", style: {
          "background-color": token("--series-1", "#2a78d6"), label: "data(label)", color: secondary,
          "font-size": 11, "text-valign": "bottom", "text-margin-y": 4, width: 14, height: 14,
          "border-width": 2, "border-color": surface,
        } },
        { selector: 'node[kind = "Class"]', style: { "background-color": token("--series-2", "#eb6834"), shape: "round-rectangle" } },
        { selector: 'node[kind = "Test"]', style: { "background-color": token("--series-3", "#1baf7a"), shape: "diamond" } },
        { selector: "node[focus = 1]", style: { width: 22, height: 22, "border-color": ink, color: ink, "font-weight": 600 } },
        { selector: "edge", style: {
          width: 1.5, "line-color": edge, "target-arrow-color": edge, "target-arrow-shape": "triangle",
          "curve-style": "bezier", "arrow-scale": 0.8, label: "data(label)", "font-size": 9, color: secondary,
        } },
        { selector: "edge[guess = 1]", style: { "line-style": "dashed" } },
        { selector: 'edge[type = "DEFINES"]', style: { "target-arrow-shape": "none", width: 1 } },
        { selector: 'edge[type = "INHERITS"]', style: { "target-arrow-shape": "triangle-backcurve", width: 2 } },
      ],
    });
    cy.on("tap", "node", (evt) => select.current(evt.target.id()));
    cy.on("mouseover", "node", (evt) => evt.target.data("label", evt.target.data("name")));
    cy.on("mouseout", "node", (evt) => {
      if (busy && evt.target.data("kind") === "Test" && evt.target.data("focus") !== 1) evt.target.data("label", "");
    });
    return () => cy.destroy();
  }, [nodes, edges, focus]);

  return (
    <div>
      <div ref={ref} className="graph" role="img" aria-label={`Call graph around ${focus}`} />
      <div className="legend">
        <span className="key"><span className="swatch-rect" style={{ background: "var(--series-1)", borderRadius: "50%" }} />function</span>
        <span className="key"><span className="swatch-rect" style={{ background: "var(--series-2)" }} />class</span>
        <span className="key"><span className="swatch-rect" style={{ background: "var(--series-3)", transform: "rotate(45deg)" }} />test</span>
        <span className="key">→ calls (solid = scip/jedi/import, dashed = name guess)</span>
        <span className="key">hover a test to see its name · arrowless lines = defines</span>
      </div>
    </div>
  );
}
