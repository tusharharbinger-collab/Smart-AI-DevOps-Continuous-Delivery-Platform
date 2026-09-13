import { useOutletContext } from "react-router-dom";
import type { AppContext } from "@/types/app-context";

export function useAppContext() {
  return useOutletContext<AppContext>();
}
