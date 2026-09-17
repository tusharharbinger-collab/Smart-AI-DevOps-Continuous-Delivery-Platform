import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Landing } from "@/pages/Landing";
import { Login } from "@/pages/Login";
import { ProjectsLayout } from "@/layouts/ProjectsLayout";
import { PipelineDashboard } from "@/pages/PipelineDashboard";
import { VerificationInspector } from "@/pages/VerificationInspector";
import { PolicyManager } from "@/pages/PolicyManager";
import { AuditLedger } from "@/pages/AuditLedger";
import { ReportsTab } from "@/pages/ReportsTab";
import { CostTab } from "@/pages/CostTab";
import { ProjectsOverview } from "@/pages/ProjectsOverview";
import { NewProject } from "@/pages/NewProject";
import { ProjectWorkspace } from "@/pages/ProjectWorkspace";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<Login />} />

        {/* Project workspaces are the only authenticated console. The four
            tab routes are ordinary screens; ProjectWorkspace supplies them
            the pipeline/run context scoped to one project. */}
        <Route path="/projects" element={<ProjectsLayout />}>
          <Route index element={<ProjectsOverview />} />
          <Route path="new" element={<NewProject />} />
          <Route path=":projectId" element={<ProjectWorkspace />}>
            <Route index element={<Navigate to="pipeline" replace />} />
            <Route path="pipeline" element={<PipelineDashboard />} />
            <Route path="verification" element={<VerificationInspector />} />
            <Route path="policy" element={<PolicyManager />} />
            <Route path="audit" element={<AuditLedger />} />
            <Route path="reports" element={<ReportsTab />} />
            <Route path="cost" element={<CostTab />} />
          </Route>
        </Route>

        {/* The classic /app console was retired once every pipeline —
            including ones registered directly through the API — became
            reachable as a project (migration 0007). Old links and bookmarks
            land on the overview instead of 404ing. */}
        <Route path="/app/*" element={<Navigate to="/projects" replace />} />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
