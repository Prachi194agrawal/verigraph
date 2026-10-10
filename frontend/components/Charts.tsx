"use client";

import {
  Bar, BarChart, CartesianGrid, LabelList, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { PIPELINE_LABEL, PIPELINE_SLOT } from "@/lib/types";

const color = (pipeline: string) => `var(--series-${PIPELINE_SLOT[pipeline] ?? 1})`;
const axis = { stroke: "var(--baseline)", tick: { fill: "var(--text-muted)", fontSize: 12 }, tickLine: false };

type Row = Record<string, string | number | null>;

function Legend({ pipelines, mark }: { pipelines: string[]; mark: "line" | "rect" }) {
  return (
    <div className="legend">
      {pipelines.map((p) => (
        <span key={p} className="key">
          <span className={mark === "line" ? "swatch-line" : "swatch-rect"} style={{ background: color(p) }} />
          {PIPELINE_LABEL[p] ?? p}
        </span>
      ))}
    </div>
  );
}

/** One tooltip lists every series at the hovered position; values lead, names follow, line keys not boxes. */
function SeriesTooltip({ active, payload, label, title }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="tooltip">
      <div className="title">{title(label)}</div>
      {payload.map((p: any) => (
        <div key={p.dataKey} className="line">
          <span className="name"><span className="swatch-line" style={{ background: color(p.dataKey) }} />
            {PIPELINE_LABEL[p.dataKey] ?? p.dataKey}</span>
          <strong>{p.value === null || p.value === undefined ? "–" : Number(p.value).toFixed(3)}</strong>
        </div>
      ))}
    </div>
  );
}

/** E2: F1 by hop depth, one 2px line per pipeline, end values labeled directly. */
export function HopChart({ points, pipelines }: { points: { pipeline: string; hop: number; f1: number | null }[];
                                                   pipelines: string[] }) {
  const hops = [...new Set(points.map((p) => p.hop))].sort((a, b) => a - b);
  const data: Row[] = hops.map((hop) => {
    const row: Row = { hop: `${hop} hop${hop > 1 ? "s" : ""}` };
    for (const p of points.filter((x) => x.hop === hop)) row[p.pipeline] = p.f1;
    return row;
  });
  if (!data.length) return <p className="muted">No impact questions with hop counts yet.</p>;
  const last = data.length - 1;
  // Direct end labels only where they cannot collide; tied series fall back to the legend + tooltip.
  const ends = pipelines
    .map((p) => ({ p, v: data[last][p] as number | null | undefined }))
    .filter((e): e is { p: string; v: number } => typeof e.v === "number")
    .sort((a, b) => b.v - a.v);
  const labeled = new Set(ends.filter((e, i) =>
    (i === 0 || ends[i - 1].v - e.v >= 0.08) && (i === ends.length - 1 || e.v - ends[i + 1].v >= 0.08)).map((e) => e.p));
  return (
    <figure className="chart" style={{ margin: 0 }}>
      <Legend pipelines={pipelines} mark="line" />
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={data} margin={{ top: 12, right: 96, bottom: 8, left: 0 }}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="hop" {...axis} />
          <YAxis domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} {...axis} axisLine={false} width={40} />
          <Tooltip content={<SeriesTooltip title={(l: string) => l} />} cursor={{ stroke: "var(--baseline)", strokeWidth: 1 }} />
          {pipelines.map((p) => (
            <Line key={p} dataKey={p} type="linear" stroke={color(p)} strokeWidth={2} isAnimationActive={false}
                  dot={{ r: 4, fill: color(p), stroke: "var(--surface-1)", strokeWidth: 2 }}
                  activeDot={{ r: 5, stroke: "var(--surface-1)", strokeWidth: 2 }} connectNulls>
              <LabelList dataKey={p} content={(props: any) => props.index === last && labeled.has(p) && props.value !== null ? (
                <text x={props.x + 10} y={props.y + 4} fill="var(--text-secondary)" fontSize={12}>
                  {PIPELINE_LABEL[p] ?? p} {Number(props.value).toFixed(2)}
                </text>) : null} />
            </Line>
          ))}
        </LineChart>
      </ResponsiveContainer>
      <figcaption className="muted">F1 on impact questions, by the number of call hops (k) the question allows.</figcaption>
    </figure>
  );
}

/** E1: F1 per question family, bars grouped by pipeline (thin bars, 4px rounded ends, 2px gaps). */
export function FamilyChart({ rows, pipelines }: { rows: { pipeline: string; f1_by_family: Record<string, number | null> }[];
                                                   pipelines: string[] }) {
  const families = [...new Set(rows.flatMap((r) => Object.keys(r.f1_by_family ?? {})))].sort();
  const data: Row[] = families.map((family) => {
    const row: Row = { family: family.replace("_", " ") };
    for (const r of rows) row[r.pipeline] = r.f1_by_family?.[family] ?? null;
    return row;
  });
  if (!data.length) return <p className="muted">No finished experiments yet.</p>;
  return (
    <figure className="chart" style={{ margin: 0 }}>
      <Legend pipelines={pipelines} mark="rect" />
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} margin={{ top: 12, right: 8, bottom: 8, left: 0 }} barGap={2} barCategoryGap="22%">
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="family" {...axis} interval={0} />
          <YAxis domain={[0, 1]} ticks={[0, 0.25, 0.5, 0.75, 1]} {...axis} axisLine={false} width={40} />
          <Tooltip shared={false} content={<SeriesTooltip title={(l: string) => l} />} cursor={{ fill: "var(--surface-2)" }} />
          {pipelines.map((p) => (
            <Bar key={p} dataKey={p} fill={color(p)} maxBarSize={24} radius={[4, 4, 0, 0]} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <figcaption className="muted">Mean set-F1 per question family. Exact values are in the leaderboard table below.</figcaption>
    </figure>
  );
}
