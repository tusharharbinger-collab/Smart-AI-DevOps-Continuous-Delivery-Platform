import { HelpCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";

const STEPS = [
  { title: "You define a pipeline", body: "Your service's build/test/deploy stages, a traffic schedule (10%→25%→50%→100%), and a verification policy — which metrics to watch, how confident it needs to be, minimum sample size, blocked deploy windows." },
  { title: "You ship a new version", body: "It deploys as a small \"canary\" cohort running alongside your currently-live \"baseline\" — not replacing it." },
  { title: "Real traffic gets split", body: "Both cohorts handle genuine production requests at the same time, so the comparison is fair." },
  { title: "Statistics compare them", body: "Error rate, latency, saturation, a business metric — each compared with the statistical test suited to it (SPRT, Mann-Whitney, CUSUM, Fisher's exact), producing a verdict with a confidence score." },
  { title: "The platform acts automatically", body: "Only within what your policy explicitly allows: healthy + confident enough → promote. A critical failure → roll back. Anything not authorized → hold for a human." },
  { title: "Every action is explained", body: "Grounded in the specific numbers that drove it, not just \"rolled back.\"" },
];

export function HowItWorksDialog() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="How this works">
          <HelpCircle className="h-4 w-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>How this platform works</DialogTitle>
        </DialogHeader>
        <ol className="space-y-3">
          {STEPS.map((step, i) => (
            <li key={step.title} className="flex gap-3">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-medium text-primary">
                {i + 1}
              </span>
              <div>
                <p className="text-sm font-medium">{step.title}</p>
                <p className="text-xs text-muted-foreground">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </DialogContent>
    </Dialog>
  );
}
