export default function Welcome() {
  return <section className="space-y-4" aria-label="Welcome to DevOps AI Agent">
    <div><h1 className="text-2xl font-semibold">DevOps AI Agent</h1><span className="text-xs border rounded-full px-2 py-1">Read-only</span></div>
    <p className="text-muted-foreground">Investigate Azure DevOps, AKS, and Terraform using read-only evidence. Include your target; each message starts a fresh investigation.</p>
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {props.cards.map(([title, description, prompt]) => <button key={title} type="button" className="text-left border rounded-xl p-4 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring" onClick={() => sendUserMessage(prompt)}>
        <h2 className="font-semibold">{title}</h2><p className="text-sm text-muted-foreground mt-1">{description}</p><p className="text-sm mt-3">{prompt}</p>
      </button>)}
    </div>
    <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">Connection and access status · Unknown</summary><p className="mt-2">{props.connections}</p></details>
  </section>;
}
