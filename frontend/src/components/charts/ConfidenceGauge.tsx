import { RadialBar, RadialBarChart, PolarAngleAxis } from "recharts";

export function ConfidenceGauge({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const color = confidence >= 0.8 ? "hsl(var(--success))" : confidence >= 0.5 ? "hsl(var(--warning))" : "hsl(var(--destructive))";

  return (
    <div className="relative h-28 w-28">
      <RadialBarChart
        width={112}
        height={112}
        cx={56}
        cy={56}
        innerRadius={40}
        outerRadius={54}
        barSize={10}
        data={[{ value: pct }]}
        startAngle={90}
        endAngle={-270}
      >
        <PolarAngleAxis type="number" domain={[0, 100]} angleAxisId={0} tick={false} />
        <RadialBar background dataKey="value" cornerRadius={6} fill={color} />
      </RadialBarChart>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-lg font-semibold">{pct}%</span>
        <span className="text-[10px] text-muted-foreground">confidence</span>
      </div>
    </div>
  );
}
