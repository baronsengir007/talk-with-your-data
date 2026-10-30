import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartSpec, Row } from "../types";

const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

export function formatValue(value: unknown): string {
  if (typeof value === "number") return number.format(value);
  if (value === null || value === undefined) return "";
  return String(value);
}

/** Revenue, price, freight and payment value are amounts in Brazilian reais. */
const MONEY = /revenue|price|freight|_value|amount|spend/;

/** A state column holds a two-letter code; the page shows the name with the code after it. */
const STATES: Record<string, string> = {
  AC: "Acre", AL: "Alagoas", AP: "Amapa", AM: "Amazonas", BA: "Bahia", CE: "Ceara",
  DF: "Distrito Federal", ES: "Espirito Santo", GO: "Goias", MA: "Maranhao", MT: "Mato Grosso",
  MS: "Mato Grosso do Sul", MG: "Minas Gerais", PA: "Para", PB: "Paraiba", PR: "Parana",
  PE: "Pernambuco", PI: "Piaui", RJ: "Rio de Janeiro", RN: "Rio Grande do Norte",
  RS: "Rio Grande do Sul", RO: "Rondonia", RR: "Roraima", SC: "Santa Catarina", SP: "Sao Paulo",
  SE: "Sergipe", TO: "Tocantins",
};

export function formatCell(column: string, value: unknown): string {
  if (column.endsWith("state") && typeof value === "string" && STATES[value.toUpperCase()]) {
    return `${STATES[value.toUpperCase()]} (${value.toUpperCase()})`;
  }
  const text = formatValue(value);
  return typeof value === "number" && MONEY.test(column) ? `R$ ${text}` : text;
}

/** A column name as a label: late_share_pct -> "late share (%)". */
export function columnLabel(name: string): string {
  const pct = name.endsWith("_pct");
  const words = (pct ? name.slice(0, -4) : name).replace(/_/g, " ");
  return pct ? `${words} (%)` : words;
}

const MAX_POINTS = 40;
const MAX_TABLE_ROWS = 12;

/** The plan's chart spec drawn from the result rows. One row draws nothing: the headline says it, the receipt holds the row. */
export default function Chart({ chart, rows }: { chart: ChartSpec | null; rows: Row[] }) {
  if (rows.length === 0) return null;
  const type = chart?.type ?? "number";
  const x = chart?.x;
  const y = chart?.y;
  const drawable =
    (type === "bar" || type === "line") &&
    rows.length > 1 &&
    !!x &&
    !!y &&
    x in rows[0] &&
    rows.every((r) => typeof r[y] === "number");
  if (!drawable) return rows.length === 1 ? null : <Table rows={rows} />;

  const data = rows.slice(0, MAX_POINTS).map((r) => ({ ...r, [x]: formatCell(x, r[x]) }));
  const tick = (v: string) => (v.length > 14 ? v.slice(0, 13) + "…" : v);
  const axes = (
    <>
      <CartesianGrid vertical={false} stroke="#e4e5e1" />
      <XAxis dataKey={x} tick={{ fontSize: 12 }} tickFormatter={tick} interval={data.length > 12 ? "preserveStartEnd" : 0} />
      <YAxis tick={{ fontSize: 12 }} tickFormatter={(v: number) => number.format(v)} width={56} />
      <Tooltip cursor={{ fill: "#eef0ec" }} formatter={(v) => formatValue(v)} labelFormatter={(l) => String(l)} />
    </>
  );
  return (
    <figure className="chart">
      <ResponsiveContainer width="100%" height={240}>
        {type === "line" ? (
          <LineChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            {axes}
            <Line type="monotone" dataKey={y} stroke="#0f6e56" strokeWidth={2} dot={data.length <= 24} isAnimationActive={false} />
          </LineChart>
        ) : (
          <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            {axes}
            <Bar dataKey={y} fill="#0f6e56" radius={[2, 2, 0, 0]} isAnimationActive={false} />
          </BarChart>
        )}
      </ResponsiveContainer>
      <figcaption>
        {columnLabel(y)} by {columnLabel(x)}
        {rows.length > MAX_POINTS ? `, first ${MAX_POINTS} of ${rows.length} rows` : ""}
      </figcaption>
    </figure>
  );
}


/** Rows the spec cannot draw (text columns, a list): a compact table. */
export function Table({ rows, max = MAX_TABLE_ROWS }: { rows: Row[]; max?: number }) {
  if (rows.length === 0) return null;
  const columns = Object.keys(rows[0]);
  const shown = rows.slice(0, max);
  return (
    <div className="table-wrap">
      <table className="rows">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{columnLabel(c)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>
              {columns.map((c) => (
                <td key={c} className={typeof r[c] === "number" ? "num" : undefined}>
                  {formatCell(c, r[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > max && <p className="table-more">First {max} of {rows.length} rows.</p>}
    </div>
  );
}
