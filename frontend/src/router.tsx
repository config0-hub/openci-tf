// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { AppShell } from "./AppShell";
import { NewRun } from "./NewRun";
import { RunDetail } from "./RunDetail";
import { RunsIndex, type RunsSearch } from "./RunsIndex";
import { AccountsPage, GatesPage, LocksPage, ReposPage } from "./AdminScreens";
import { NotFoundProcedure, PipelinesPage } from "./PlaceholderScreens";
import { validateCursorSearch } from "./Pagination";

const rootRoute = createRootRoute({ component: AppShell, notFoundComponent: NotFoundProcedure });

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  validateSearch: (search: Record<string, unknown>): RunsSearch => ({
    trigger_id: typeof search.trigger_id === "string" ? search.trigger_id : undefined,
    repo: typeof search.repo === "string" ? search.repo : undefined,
    ...validateCursorSearch(search),
  }),
  component: RunsIndex,
});

const newRunRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/new",
  component: NewRun,
});

const runDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$runId",
  component: RunDetail,
});

const staticRoutes = [
  createRoute({ getParentRoute: () => rootRoute, path: "/pipelines", component: PipelinesPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/repos", validateSearch: validateCursorSearch, component: ReposPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/accounts", validateSearch: validateCursorSearch, component: AccountsPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/locks", validateSearch: validateCursorSearch, component: LocksPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/gates", validateSearch: validateCursorSearch, component: GatesPage }),
];

const routeTree = rootRoute.addChildren([runsRoute, newRunRoute, runDetailRoute, ...staticRoutes]);
export const router = createRouter({ routeTree, defaultPreload: "intent" });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
