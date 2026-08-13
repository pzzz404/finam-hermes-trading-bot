/**
 * @name Clear-text logging of sensitive information
 * @description Detects sensitive data written to logging and print sinks, while
 *              recognizing the repository's tested environment-value redaction.
 * @kind path-problem
 * @problem.severity error
 * @security-severity 7.5
 * @precision high
 * @id finam/clear-text-logging-sensitive-data
 * @tags security external/cwe/cwe-312 external/cwe/cwe-359 external/cwe/cwe-532
 */

import python
private import semmle.python.dataflow.new.DataFlow
import semmle.python.dataflow.new.TaintTracking
import semmle.python.ApiGraphs
import semmle.python.security.dataflow.CleartextLoggingCustomizations

class RedactedEnvironmentValue extends CleartextLogging::Sanitizer {
  RedactedEnvironmentValue() {
    this =
      API::moduleImport("finam_trading_bot.redaction")
          .getMember("redact_environment_values")
          .getACall()
          .getReturn()
  }
}

private module CleartextLoggingWithRedactionConfig implements DataFlow::ConfigSig {
  predicate isSource(DataFlow::Node source) { source instanceof CleartextLogging::Source }

  predicate isSink(DataFlow::Node sink) { sink instanceof CleartextLogging::Sink }

  predicate isBarrier(DataFlow::Node node) { node instanceof CleartextLogging::Sanitizer }

  predicate observeDiffInformedIncrementalMode() { any() }
}

module CleartextLoggingWithRedactionFlow =
  TaintTracking::Global<CleartextLoggingWithRedactionConfig>;

import CleartextLoggingWithRedactionFlow::PathGraph

from
  CleartextLoggingWithRedactionFlow::PathNode source,
  CleartextLoggingWithRedactionFlow::PathNode sink,
  string classification
where
  CleartextLoggingWithRedactionFlow::flowPath(source, sink) and
  classification = source.getNode().(CleartextLogging::Source).getClassification()
select sink.getNode(), source, sink, "This expression logs $@ as clear text.", source.getNode(),
  "sensitive data (" + classification + ")"
