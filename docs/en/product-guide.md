# Product, users and market evidence

Code scope aligned on 2026-11-13; market evidence retains its 2026-11-04 review. Product facts, external industry signals and commercial hypotheses are separate evidence categories. See the [workbench guide](workbench-guide.md) for navigation, [current capability matrix](current-capabilities.md) for execution limits, and [technical guide](technical-guide.md) for implementation.

## What is ForgeFlow?

ForgeFlow helps production planners repair existing purchasing and manufacturing plans after a supplier cancellation or workcenter outage. It identifies affected customer orders and compares using available stock, buying replacements or changing manufacturing arrangements against deadlines, cost and capacity. An authorized person selects and approves a specific plan before the system applies it and checks the resulting purchasing and production records.

## How is production planning different from exception recovery?

Production planning answers what to make, how much to make, when to make it, and how stock, purchasing and manufacturing should be combined to meet customer orders. It normally starts from demand and resources and establishes or re-plans an overall schedule.

Production-plan exception recovery starts from an existing plan after a supply or manufacturing input fails. It tries to retain valid arrangements and repair only the affected part. ForgeFlow addresses the latter problem: it does not replace from-scratch factory scheduling or automatically re-plan every product and shared capacity. Current Odoo 20 code and historical synthetic engineering records cover bounded multi-product procurement, multi-level BOM and multiple-MO recovery. See the [current matrix](current-capabilities.md) for versions, input limits and unverified acceptance; this does not establish customer production readiness.

## Who uses it, decides and might pay?

The primary daily user is a production planner preparing a recovery decision. Sales owns customer orders and delivery commitments; procurement owns supplier and purchase-order facts and helps identify the cancelled supply commitment; production staff provide outage, capacity and actual-production facts; the planner organizes orders, stock, purchasing and manufacturing constraints into a recovery proposal; an authorized company approver selects and approves a specific plan. Not every planner necessarily holds approval authority, and chat does not decide for them. A potential buyer might own a production, operations or supply-chain budget, with IT/ERP staff evaluating data access and security. This buyer profile is a workflow-based hypothesis, not an interviewed or paying customer profile. User, approver and payer may or may not be the same person.

## Which companies should be considered for a pilot?

The initial customer segment is a hypothesis: manufacturing teams with repeated cancellations or outages, substantial manual reconciliation, usable ERP records, a clear approval owner and willingness to measure a pilot. Start with a narrow supported process rather than claiming whole-factory coverage. Odoo 20 has historical synthetic records for multi-product procurement and multi-level BOM / multiple-MO recovery. Input size, unit, calendar and operation limits, and target customer acceptance, remain scoped by the [current matrix](current-capabilities.md). Whole-factory scheduling and plug-and-play integration with any ERP are not established capabilities. Configured Odoo source polling is a separate, bounded capability. Implementation and acceptance currently use Odoo sandboxes; other ERP integrations require separate assessment. Company size or industry alone does not establish buying fit.

## What concrete problem does it solve?

The problem is repairing existing commitments after a disruption without losing track of affected orders, feasible alternatives or the changes actually made:
- Trace impact through recorded purchasing, manufacturing and sales relationships; missing links stay unknown rather than making every order directly affected.
- Compare available stock, finished-goods purchases, manufacturing or a mix against deadlines, budgets, margin requirements, supplier limits and capacity. The default prioritizes retaining valid existing arrangements before cost; it is not always the cheapest route.
- Let an authorized person review, select and approve a specific version, then apply controlled changes, read ERP records back and inspect original acceptance. Approval, changed records and actual receipt/delivery are different states. Chat cannot select, approve or execute.

For example, a cancelled supply commitment raises concrete questions: which customer orders depend on it, how much stock can cover the gap, and whether another supplier or manufacturing can satisfy the constraints. An outage instead starts from unavailable manufacturing supply. Changing supply or production arrangements does not establish support for material substitution or changing the bill of materials. These are workflow examples, not measured customer outcomes or a guarantee that a feasible plan always exists.

## Why add it to an ERP, APS or general chatbot?

A company may not need another product. ERP and advanced planning and scheduling (APS) capabilities depend on configuration; they are not merely passive record stores and may already handle recovery. ForgeFlow focuses on a narrow workflow connecting impact evidence, constrained alternatives, plan-bound approval and checked execution. That describes its implementation, not an exclusive feature or superiority claim. If existing systems already do this well, incremental value must be measured. A general chatbot without business data or controlled tools can only advise; a tool-connected chatbot may query ERP or perform tasks. Compare actual permissions, constraints, approval and verification, not whether a product has a chat interface. RPA executes scripted, repetitive system actions; it does not prepare constrained recovery plans or bind approval to a specific plan version, so it is not a substitute for this workflow.

## Are there real customers and commercial readiness?

Reviewed evidence comes from synthetic ERP-Bench recovery tasks, Odoo sandboxes and product/research tests, not customer-factory operations or live telemetry. Workspace case counts, approval queues, run counts and passing verifications do not establish adoption, market demand or buying intent. Current records do not establish production customers, paid contracts, revenue, renewals or commercial scale; this is not an absolute claim that no customer could exist.

## Measured savings, time and ROI

Enterprise financial savings, labor-time improvements and return on investment (ROI) have not been measured. A synthetic planned-cost difference is not an actual expenditure reduction, cannot be extrapolated to other factories and does not prove every disruption saves money. Plan, approval, execution and acceptance records are not customer-outcome measurements. Proposed evaluation would compare actual spending, reconciliation time and outcomes against the existing process, accounting for integration and maintenance costs; this is a method, not completed measurement.

## What supports market demand?

For manufacturing disruption recovery after supplier cancellations or outages, industry data offers indirect market-demand signals, not validation from paying customers of ForgeFlow. [Deloitte's 2025 manufacturing survey](https://www.deloitte.com/us/en/insights/industry/manufacturing-industrial-products/2025-smart-manufacturing-survey.html), published May 1, 2025, surveyed 600 executive respondents (a people count, not a verified company count) at large manufacturers with US headquarters or operations in August-September 2024; 35% ranked advanced production scheduling first or second for system investment over the next two years. [McKinsey's 2025 supply-chain survey](https://www.mckinsey.com/capabilities/operations/our-insights/supply-chain-risk-survey), published December 2, 2025, covered 100 companies/global supply-chain leaders; the share planning major digital supply-chain investment fell from 47% the previous year to 25%, citing costs, management resources and competing ERP priorities. These support interest in planning and a budget constraint, respectively, not Chinese SME demand, ForgeFlow sales, ROI or a market growth rate. These are public reports reviewed on November 4, 2026, not live market monitoring.

## Market outlook: opportunity, barriers and conclusion

The opportunity is conditional: factories repeatedly reconciling records and coordinating recovery after cancellations or outages might benefit from faster review-ready plans and fewer missed dependencies or mistaken changes. That is a value hypothesis. Barriers include ERP integration and data quality, existing ERP/APS and manual alternatives, trust and accountability for changes, deployment/security requirements, and implementation and maintenance cost. Current evidence supports testing a focused use case, not claiming large paid demand or repeatable sales. Industry interest, a working demonstration, willingness to pay and a sustainable sales process are different milestones.

## Market size and growth rate

The reviewed material contains no verified ForgeFlow-specific market-size or annual-growth estimate, so it cannot support a concrete figure. The whole ERP, AI or smart-manufacturing market is not this product's addressable market, and a survey percentage change is not market growth. A bottom-up estimate would first define region, company type and supported use cases, then verify eligible-company counts, reachable share and willingness to pay annually; missing inputs remain unknown.

## What is the business model and current pricing?

The reviewed material does not document a confirmed official price or validated business model. It cannot establish current charging arrangements or whether there are paying customers; missing records do not mean there are no customers or that the product is free. A paid pilot followed by software subscription and necessary integration/maintenance services is one business-model hypothesis to test, not an existing offer, price, revenue stream or sales commitment.

## How should demand and commercial prospects be validated?

Proposed next steps, not completed work: interview planners, procurement/production collaborators, approvers and budget owners about disruption frequency, existing ERP/APS alternatives, manual effort and buying barriers. Use an authorized, narrowly scoped real-data pilot to compare time to a review-ready plan, reconciliation effort, recommendation adoption, mistaken changes/rework and integration/maintenance cost against the current process. Actual savings require actual expenditure and outcome evidence, not just comparing two plans. Paid pilots, purchasing commitments and continued use/renewals would test willingness to pay and repeatable sales; verbal interest and demo counts are not purchases.
