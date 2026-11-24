# ADK Go Workflow API

> Requires `google.golang.org/adk/v2 >= v2.0.0`, which is where the `workflow` package and
> `agent/workflowagent` first ship.

The graph runtime in **`google.golang.org/adk/v2/workflow`**, for the cases the linear
`sequentialagent` / `parallelagent` / `loopagent` composition in `adk-go.md` cannot express:
conditional routing, fan-out with a fan-in barrier, per-node retries or timeouts, or a workflow
that pauses for human input and resumes later. For a plain chain of agents, those workflow
*agents* are simpler.

Read `adk-go.md` first for the base API.

**Official docs:** [Workflows overview](https://adk.dev/workflows/index.md) ·
[pkg.go.dev/workflow](https://pkg.go.dev/google.golang.org/adk/v2/workflow) ·
[runnable examples](https://github.com/google/adk-go/tree/main/examples/workflow)

## 1. Core concepts

A `Workflow` is a graph-based agent: nodes do work, edges define flow, `workflow.Start` is the entry point.

```go
import (
	"strings"

	"google.golang.org/adk/v2/agent"
	"google.golang.org/adk/v2/agent/workflowagent"
	"google.golang.org/adk/v2/workflow"
)

cfg := workflow.NodeConfig{RetryConfig: workflow.DefaultRetryConfig()}

upper := workflow.NewFunctionNode("upper",
	func(_ agent.Context, in string) (string, error) {
		return strings.ToUpper(in), nil
	}, cfg)

suffix := workflow.NewFunctionNode("suffix",
	func(_ agent.Context, in string) (string, error) {
		return in + " IS AWESOME!", nil
	}, cfg)

rootAgent, err := workflowagent.New(workflowagent.Config{
	Name:        "simple_sequence_workflow",
	Description: "Uppercases a string and appends a suffix",
	Edges:       workflow.Chain(workflow.Start, upper, suffix),
})
```

The first node receives the user's message as its input. `workflowagent.New` returns a plain
`agent.Agent`.

## Node kinds

Every constructor takes a `NodeConfig` (retries, timeout — see *Retries, timeouts, errors*) and
returns a value you drop into an `Edge`.

### `NewFunctionNode` — a typed transform

```go
upper := workflow.NewFunctionNode("upper",
	func(_ agent.Context, in string) (string, error) {
		return strings.ToUpper(in), nil
	}, workflow.NodeConfig{})
```

The return value becomes `Event.Output` and arrives as the successor's typed input.

### `NewEmittingFunctionNode` — emit events, then return

Use it when the node must produce something *besides* its return value: user-visible progress, a
state delta, a routing tag, or a HITL prompt.

```go
progress := workflow.NewEmittingFunctionNode("progress",
	func(ctx agent.Context, in string, emit func(*session.Event) error) (any, error) {
		ev := session.NewEvent(ctx, ctx.InvocationID())
		// Content renders in the UI; Output is what the next node receives.
		ev.Content = genai.NewContentFromText("working…", genai.RoleModel)
		if err := emit(ev); err != nil {
			return nil, err
		}
		// Returning a non-nil value emits a terminal event carrying it as Output.
		// Returning nil instead suppresses that terminal event entirely — which is
		// what you want when an event you already emitted carries the output.
		return in, nil
	}, workflow.NodeConfig{})
```

**What a function node's return value becomes:** a `*session.Event` is yielded as-is (this is how
you set `Event.Routes`), a `*genai.Content` becomes `event.Content`, and anything else becomes
`event.Output`.

### `NewAgentNode` — wrap an agent

```go
drafter, err := workflow.NewAgentNode(draftAgent, workflow.NodeConfig{})
// NewAgentNodeTyped[In, Out](draftAgent, cfg) instead reflects In/Out into JSON
// schemas, so the agent's input and reply are validated against your structs.
```

An `LlmAgent` with unset `Mode` defaults to single-turn here. Register the wrapped agent in
`workflowagent.Config.SubAgents` so event authors resolve.

### `NewToolNode` — call a tool as a step

```go
lookup, err := workflow.NewToolNode(weatherTool, workflow.NodeConfig{})
// NewNamedToolNode("weather_step", weatherTool, cfg) to override the node name,
// which otherwise comes from the tool.
```

### `NewWorkflowNode` — nest a sub-workflow

```go
sub, err := workflow.NewWorkflowNode("subflow", workflow.Chain(workflow.Start, upper, suffix))
```

`NewJoinNode`, `NewDynamicNode` and `NewParallelWorker` are the concurrency kinds — see
*Parallelism and fan-in*.

## Edges and routing

An edge with no `Route` always fires. A routed edge fires only when the source node tagged its
event with a matching value:

```go
edges := []workflow.Edge{
	{From: workflow.Start, To: triage},
	{From: triage, To: answer, Route: workflow.StringRoute("question")},
	{From: triage, To: escalate, Route: workflow.IntRoute(2)},
	{From: triage, To: archive, Route: workflow.BoolRoute(false)},
	{From: triage, To: urgent, Route: workflow.MultiRoute[string]{"p0", "p1"}},
	{From: triage, To: fallback, Route: workflow.Default},
}
```

Every route is compared as a string against the entries in `Event.Routes`, so `IntRoute(2)`
matches the entry `"2"` and `BoolRoute(false)` matches `"false"`. Any type with a
`Matches(*session.Event) bool` method works as a route.

**A node signals its branch by setting `Event.Routes []string`** on an event it emits:

```go
func classifyAndRoute(ctx agent.Context, msg string, emit func(*session.Event) error) (any, error) {
	ev := session.NewEvent(ctx, ctx.InvocationID())
	ev.Routes = []string{classify(msg)} // e.g. "question"
	ev.Output = msg                     // feeds the successor's typed input
	if err := emit(ev); err != nil {
		return nil, err
	}
	return nil, nil // nil output suppresses the default terminal event
}

triage := workflow.NewEmittingFunctionNode("triage", classifyAndRoute, workflow.NodeConfig{})
```

### Building edge sets

`EdgeBuilder` is the readable way to express fan-out and fan-in:

```go
eb := workflow.NewEdgeBuilder()
eb.Add(workflow.Start, triage)
eb.AddRoute(triage, answer, workflow.StringRoute("question"))
eb.AddRoutes(triage, map[string]workflow.Node{ // shorthand for several StringRoutes
	"statement":   comment,
	"exclamation": react,
})
eb.AddFanOut(workflow.Start, alpha, beta, gamma) // one source, many targets
eb.AddFanIn(gather, alpha, beta, gamma)          // target FIRST, then its predecessors
edges := eb.Build()
```

`workflow.Chain(a, b, c)` builds the unconditional edges of a straight line, and returns nil for
fewer than two nodes. `workflow.Concat(...)` flattens `Edge` values and `[]Edge` slices into one
slice — handy for gluing a chain onto a routed fan-out — and silently drops anything else.

### Dispatch rules

Which successors fire is decided per activation, and the edge set is the clearest place to see it:

```go
edges := []workflow.Edge{
	{From: workflow.Start, To: triage},

	// Unconditional: always fires, whatever the node tagged.
	{From: triage, To: audit},

	// Concrete routes: fire only on a matching tag in Event.Routes.
	{From: triage, To: answer, Route: workflow.StringRoute("question")},
	{From: triage, To: urgent, Route: workflow.MultiRoute[string]{"p0", "p1"}},

	// Default fires when no *concrete* route matched. The unconditional edge
	// above does NOT count as a match, so a run tagged "other" fires both
	// `audit` and `fallback` — and if both then end the graph with output, the
	// run fails with ErrMultipleTerminalOutputs. Converge them on a JoinNode.
	{From: triage, To: fallback, Route: workflow.Default},
}

// Two edges to the same target within one activation are deduplicated.
// If every outgoing edge is routed and none match, with no Default, the graph
// silently dead-ends here — treated as a deliberate decision, not an error.
```

Inside the node body the corresponding limits are one routing event and one output per
activation:

```go
func routeOnce(ctx agent.Context, msg string, emit func(*session.Event) error) (any, error) {
	ev := session.NewEvent(ctx, ctx.InvocationID())
	ev.Routes = []string{"question"} // a second event with Routes -> ErrMultipleRoutingEvents
	ev.Output = msg                  // a second event with Output -> ErrMultipleOutputs
	if err := emit(ev); err != nil {
		return nil, err
	}
	return nil, nil
}
```

## Parallelism and fan-in

Three separate mechanisms — pick deliberately.

### (a) Graph fan-out to a `JoinNode`

Multiple edges from one source run concurrently. The join activates exactly once, after every
declared predecessor completes, and its input is a `map[string]any` keyed by predecessor node name.

```go
alpha := workflow.NewFunctionNode("alpha",
	func(_ agent.Context, _ string) (string, error) { return "A", nil }, workflow.NodeConfig{})
beta := workflow.NewFunctionNode("beta",
	func(_ agent.Context, _ string) (string, error) { return "B", nil }, workflow.NodeConfig{})

gather := workflow.NewJoinNode("gather")
format := workflow.NewFunctionNode("format_summaries",
	func(_ agent.Context, gathered map[string]any) (string, error) {
		return fmt.Sprintf("alpha=%v beta=%v", gathered["alpha"], gathered["beta"]), nil
	}, workflow.NodeConfig{})

eb := workflow.NewEdgeBuilder()
eb.AddFanOut(workflow.Start, alpha, beta)
eb.AddFanIn(gather, alpha, beta) // target FIRST, then its predecessors
eb.Add(gather, format)

joinAgent, err := workflowagent.New(workflowagent.Config{
	Name: "fan_in", Edges: eb.Build(),
}) // one turn yields "alpha=A beta=B"
```

A predecessor that completed with no output contributes `nil` — the barrier counts completions,
not outputs. **Never route conditionally into a `JoinNode`**: the barrier waits for every declared
predecessor, and a route-skipped one never fires, so the graph hangs.

### (b) `ParallelWorker` — map one node over a slice

One goroutine per item, bounded by the worker's own concurrency limit. It yields a single event
whose `Output` is a positionally ordered `[]any`.

```go
// The wrapped node handles ONE item; the worker runs it over the whole slice.
score := workflow.NewFunctionNode("score",
	func(_ agent.Context, city string) (string, error) {
		return city + ":ok", nil
	}, workflow.NodeConfig{})

fanout, err := workflow.NewParallelWorker("score_all", score, 4, workflow.NodeConfig{})
if err != nil {
	return nil, err
}

split := workflow.NewFunctionNode("split",
	func(_ agent.Context, in string) ([]string, error) {
		return strings.Fields(in), nil // the slice the worker maps over
	}, workflow.NodeConfig{})

join := workflow.NewFunctionNode("join_results",
	func(_ agent.Context, results []any) (string, error) {
		parts := make([]string, len(results))
		for i, r := range results {
			parts[i] = fmt.Sprint(r) // positional: input order preserved
		}
		return strings.Join(parts, " "), nil
	}, workflow.NodeConfig{})

workerAgent, err := workflowagent.New(workflowagent.Config{
	Name:  "parallel_worker",
	Edges: workflow.Chain(workflow.Start, split, fanout, join),
}) // "paris rome" yields "paris:ok rome:ok"
```

Fail-fast: the first non-retryable error cancels in-flight workers. The wrapped node must not
carry a `RetryConfig` — retries belong to the worker, applied per item.

### (c) Dynamic nodes — orchestrate in Go

A dynamic node calls `RunNode` per child instead of declaring a static edge set, so the fan-out
can depend on the input. Children run concurrently if you launch them concurrently.

```go
hello := workflow.NewFunctionNode("hello_node",
	func(_ agent.Context, name string) (string, error) {
		return "Hello " + name, nil
	}, workflow.NodeConfig{})

orchestrator := workflow.NewDynamicNode[string, string]("orchestrate",
	func(ctx agent.Context, in string, _ func(*session.Event) error) (string, error) {
		// Decide at run time how many children to run, and with what input.
		var out []string
		for _, name := range strings.Fields(in) {
			got, err := workflow.RunNode[string](ctx, hello, name)
			if err != nil {
				return "", err
			}
			out = append(out, got)
		}
		return strings.Join(out, "; "), nil
	}, workflow.NodeConfig{})

dynAgent, err := workflowagent.New(workflowagent.Config{
	Name:  "dynamic",
	Edges: workflow.Chain(workflow.Start, orchestrator),
}) // "ada grace" yields "Hello ada; Hello grace"
```

`WithMaxConcurrency(n)` caps graph-scheduled nodes per run; nodes over the cap queue FIFO. It
deliberately does **not** apply to dynamic sub-nodes invoked via `RunNode`, which are awaited
inline by the parent — gating them would deadlock.

## Passing data between nodes

**Edge-passed values are `any`.** Typing is per-node generics, not a typed graph: nothing checks
at compile time that one node's output matches the next node's input. The engine reconciles them
at run time — it type-asserts the incoming `any` to `IN`, and on failure falls back to a JSON
round-trip.

That fallback is what lets an untyped payload land in a typed parameter. Here the producer knows
nothing about `Itinerary`, and it still arrives populated:

```go
type Itinerary struct {
	Origin string `json:"origin"`
	Days   int    `json:"days"`
}

produce := workflow.NewFunctionNode("produce",
	func(_ agent.Context, _ string) (map[string]any, error) {
		return map[string]any{"origin": "SFO", "days": 3}, nil
	}, workflow.NodeConfig{})

consume := workflow.NewFunctionNode("consume",
	func(_ agent.Context, in Itinerary) (string, error) {
		return fmt.Sprintf("origin=%s days=%d", in.Origin, in.Days), nil // origin=SFO days=3
	}, workflow.NodeConfig{})
```

**Session state** is the other channel. Write via `Event.Actions.StateDelta`:

```go
ev := session.NewEvent(ctx, ctx.InvocationID())
ev.Actions.StateDelta = map[string]any{"progress": "halfway"}
if err := emit(ev); err != nil {
	return nil, err
}
```

Read it back through the context:

```go
lang, err := ctx.Session().State().Get("progress")
```

`Input`, `Output` and interrupt payloads must be **JSON-encodable** to survive pause/resume.
For binary data, store an artifact and carry its URI.

## Retries, timeouts, errors

`NodeConfig` carries the per-node execution policy:

```go
retry := workflow.DefaultRetryConfig() // 5 attempts, 1s → 60s, factor 2.0, jitter 1.0
retry.MaxAttempts = 3
retry.ShouldRetry = func(err error) bool { return !errors.Is(err, errFatal) }

cfg := workflow.NodeConfig{
	RetryConfig: retry,
	Timeout:     30 * time.Second, // bounds a single activation

	// RerunOnResume: &rerun, // &true re-runs the node after HITL; nil/&false hands off
	// WaitForOutput: &wait,  // park until every predecessor has triggered this node
	// EmitsOwnSpan:  true,   // the node emits its own tracing span
}
```

`DefaultRetryConfig()` does not retry `ErrInputValidation` — the input is deterministic per
activation, so retrying cannot help.

**Construction-time validation** is strict, so a malformed graph fails at `New` rather than
mid-run:

1. `Start` must exist and have no incoming edges (`ErrNoStartNode`, `ErrNodePointsToStart`)
2. All nodes must be reachable from `Start` (`ErrNodesNotReachable`)
3. No duplicate node names (`ErrDuplicateNodeName`), and no duplicate `(From, To)` pairs
   regardless of route (`ErrDuplicateEdge`) — use `MultiRoute` instead
4. At most one `Default` route per node (`ErrMultipleDefaultRoutes`)
5. No unconditional cycles — a cycle needs at least one routed edge (`ErrUnconditionalCycle`)
6. Two or more unconditional incoming edges on a non-`JoinNode` target is rejected
   (`ErrUnsupportedFanIn`): "use a JoinNode to converge branches"

## Human-in-the-loop

HITL pauses a run to ask the user something and resumes it on a later turn. 

**Two resume modes**, chosen by `NodeConfig.RerunOnResume`.

**Handoff** (`nil` or `&false`) — the reply is routed to the asker's *successors* as their
input, and the asker does not re-run:

```go
ask := workflow.NewEmittingFunctionNode[any, any]("ask_name",
	func(ctx agent.Context, _ any, emit func(*session.Event) error) (any, error) {
		if err := emit(workflow.NewRequestInputEvent(ctx, session.RequestInput{
			InterruptID: "ask_name-" + uuid.NewString(),
			Message:     "What's your name?",
		})); err != nil {
			return nil, err
		}
		return nil, workflow.ErrNodeInterrupted
	},
	workflow.NodeConfig{},
)

greet := workflow.NewFunctionNode("greet",
	func(_ agent.Context, name string) (string, error) {
		return fmt.Sprintf("Hello, %s!", name), nil
	}, workflow.NodeConfig{})

edges := workflow.Chain(workflow.Start, ask, greet) // the reply becomes greet's input
```

**Re-entry** (`&true`) — the asker re-runs from scratch. `ResumeOrRequestInput` collapses both
passes into one call: it asks and pauses the first time, and returns the human's reply the
second:

```go
rerun := true
greet := workflow.NewEmittingFunctionNode[any, any]("greet",
	func(nc agent.Context, _ any, emit func(*session.Event) error) (any, error) {
		reply, err := workflow.ResumeOrRequestInput(nc, emit, session.RequestInput{
			// Stable across this run's re-entry, unique per run.
			InterruptID: "ask_name-" + nc.InvocationID(),
			Message:     "What's your name?",
		})
		if err != nil {
			return nil, err
		}
		name, _ := reply.(string)
		return fmt.Sprintf("Hello, %s!", name), nil
	},
	workflow.NodeConfig{RerunOnResume: &rerun},
)
```

`workflowagent` wires resumption automatically: on each turn it inspects inbound content for
`adk_request_input` function responses, rebuilds the run state from session history, and
dispatches to `Workflow.Resume` instead of `Workflow.Run`. Run state lives in `session.State`,
not on the agent, so one agent instance safely serves many concurrent sessions.

A resume consumes the pending request. Submitting the same response twice does not re-run the
node, but the second call yields `workflow.ErrNothingToResume`.
Handoff successors are matched with no event, so a successor reachable only via a *concrete*
route does **not** fire on resume — only unconditional edges and `Default`.

## See also

- `adk-go.md` — the base ADK Go API (agents, tools, models, sessions, runner)

Upstream examples: [`examples/workflow/`](https://github.com/google/adk-go/tree/main/examples/workflow)
— `basic`, `routing/string`, `hitl_simple`, `hitl_rerun`, `dynamic/basic`, `complex`.
