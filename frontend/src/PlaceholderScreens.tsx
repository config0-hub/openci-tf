import { Link } from "@tanstack/react-router";

export function NotFoundProcedure() {
  return (
    <main className="procedure-page">
      <section className="abnormal-block not-found-procedure" role="alert" aria-labelledby="not-found-title">
        <div className="procedure-code">ABNORMAL PROCEDURE · ROUTE NOT FILED</div>
        <h1 id="not-found-title">CONSOLE PROCEDURE NOT FOUND</h1>
        <p>THE REQUESTED ROUTE IS NOT PART OF THIS QUICK REFERENCE.</p>
        <Link to="/" className="procedure-return">← RETURN TO RUN PROCEDURES</Link>
      </section>
    </main>
  );
}

export function PipelinesPage() {
  return (
    <main className="procedure-page">
      <header className="page-heading">
        <div>
          <div className="procedure-code">STAGED CHECKLIST</div>
          <h1>EXTENDED PROCEDURES</h1>
        </div>
        <div className="retention-stamp">NOT YET<br />AVAILABLE</div>
      </header>
      <p className="fixed-measure">
        MULTI-STAGE RUNS EXECUTING FOLDERS IN SEQUENCE. NOT YET AVAILABLE.
      </p>
      <ol className="pipeline-schematic" aria-label="Sequential pipeline concept">
        <li><strong>STAGE 1</strong><span>FOLDER PROCEDURE</span></li>
        <li><strong>STAGE 2</strong><span>FOLDER PROCEDURE</span></li>
        <li><strong>STAGE 3</strong><span>FOLDER PROCEDURE</span></li>
      </ol>
      <p className="fixed-measure">SEQUENTIAL ORDER ONLY · CONCURRENCY REMAINS UNDECIDED.</p>
    </main>
  );
}
