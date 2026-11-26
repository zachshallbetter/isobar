# Independent IBS-0004 replay using exact Ruby rationals and decimal logarithms.
require_relative "verify_evidence"
require_relative "replay_bundle"
require "bigdecimal"
require "bigdecimal/math"
require "digest"
require "stringio"
require "zlib"

VERSIONS = {
  ratio: "total-byte-ratio-v1",
  throughput: "serial-bytes-per-second-v1",
  composite: "weighted-log-ratio-v1",
  ordinal: "enabled-lexicographic-competition-v1"
}.freeze
WEIGHT_TOTAL = 10_000
PRECISION = 40
MAX_ORIGINAL = 1024 * 1024
MAX_ARTIFACT = 2 * MAX_ORIGINAL

def weighted_score(terms)
  weights = terms.map { |term| term.fetch(:weight) }
  return "invalid_weights" unless weights.all? { |weight| weight.finite? && weight >= 0 } &&
                                  (weights.sum - 1).abs <= 1e-12

  total = BigDecimal("0")
  terms.each do |term|
    weight = term.fetch(:weight)
    next if weight.zero?

    candidate = term[:candidate]
    reference = term[:reference]
    return "unavailable" unless [candidate, reference].all? { |value| value.is_a?(Numeric) && value.finite? && value.positive? }

    direction = { "max" => 1, "min" => -1 }.fetch(term.fetch(:direction))
    decimal_weight = weight.is_a?(Rational) ? BigDecimal(weight, PRECISION) : BigDecimal(weight.to_s)
    total += direction * decimal_weight * (decimal_log(candidate) - decimal_log(reference))
  end
  result = (BigMath.exp(total, PRECISION) * 100).to_f
  result.finite? && result.positive? ? result : "out_of_range"
end

def decimal_log(value)
  if value.is_a?(Rational)
    BigMath.log(BigDecimal(value.numerator), PRECISION) -
      BigMath.log(BigDecimal(value.denominator), PRECISION)
  else
    BigMath.log(BigDecimal(value.to_s), PRECISION)
  end
end

def gzip_matches(artifact, original)
  offset = 0
  output = "".b
  while offset < artifact.bytesize
    input = StringIO.new(artifact.byteslice(offset..))
    reader = Zlib::GzipReader.new(input)
    begin
      output << (reader.read(original.bytesize - output.bytesize + 1) || "".b)
      raise InvalidEvidence, "Gzip output exceeds supplied input" if output.bytesize > original.bytesize

      consumed = input.pos - (reader.unused&.bytesize || 0)
      raise InvalidEvidence, "Incomplete gzip member" unless reader.eof? && consumed.positive?

      offset += consumed
    ensure
      reader.finish
    end
  end
  output == original
rescue Zlib::Error, EOFError => error
  raise InvalidEvidence, "Invalid gzip artifact: #{error.class}"
end

def artifact_counts(source_path, artifact_path)
  original = (File.binread(source_path, MAX_ORIGINAL + 1) || "".b)
  artifact = (File.binread(artifact_path, MAX_ARTIFACT + 1) || "".b)
  decoded_counts(original, artifact)
end

def decoded_counts(original, artifact)
  if original.bytesize > MAX_ORIGINAL || artifact.bytesize > MAX_ARTIFACT
    raise InvalidEvidence, "Artifact or original exceeds the local format limits"
  end

  exact = if artifact.start_with?("\x1f\x8b".b)
            gzip_matches(artifact, original)
          elsif artifact.start_with?("\x28\xb5\x2f\xfd".b) ||
                (artifact.bytesize >= 4 && (artifact.getbyte(0) & 0xf0) == 0x50 &&
                  artifact.byteslice(1, 3) == "\x2a\x4d\x18".b)
            require_relative "zstandard_artifact"
            zstandard_matches(artifact, original)
          else
            raise InvalidEvidence, "Unrecognized gzip or Zstandard artifact"
          end
  raise InvalidEvidence, "Artifact did not reconstruct the supplied input" unless exact

  [original.bytesize, artifact.bytesize, Digest::SHA256.hexdigest(original)]
end

def slot(row)
  row.values_at("worker_id", "block_id", "input_id")
end

def collect_artifacts(arguments, measurements, qualified)
  supplied = Hash.new { |hash, key| hash[key] = [] }
  arguments.each do |id, source, artifact|
    raise InvalidEvidence, "Artifact requires a qualified measurement observation" unless qualified[id]

    entries = supplied[id]
    if entries.length >= measurements.fetch(id).fetch("invocation_count")
      raise InvalidEvidence, "Too many artifact pairs for observation"
    end
    entries << artifact_counts(source, artifact)
  end
  supplied.each do |id, entries|
    row = measurements.fetch(id)
    size = entries.sum(&:first)
    if size > row["work_bytes"] || (entries.length == row["invocation_count"] && size != row["work_bytes"])
      raise InvalidEvidence, "Artifact source byte total disagrees with observation"
    end
  end
  supplied
end

def cohort_measurements(rows, supplied, source_digests)
  work = rows.sum { |row| row.fetch("work_bytes") }
  elapsed = rows.sum { |row| row.fetch("elapsed_ns") }
  pieces = rows.map do |row|
    entries = supplied.fetch(row.fetch("observation_id"), [])
    next if entries.length != row["invocation_count"]

    digests = entries.map(&:last).sort
    key = slot(row)
    source_digests[key] ||= digests
    raise InvalidEvidence, "Supplied source contents disagree within the paired slot" if source_digests[key] != digests

    entries.sum { |entry| entry[1] }
  end
  compressed = pieces.sum unless pieces.any?(&:nil?)
  ratio = Rational(work, compressed) if work.positive? && compressed&.positive?
  throughput = Rational(work * 1_000_000_000, elapsed)
  diagnostic = {
    work_bytes: work.to_s, elapsed_ns: elapsed.to_s, artifact_bytes: compressed&.to_s,
    metrics: {
      ratio: ratio ? { value: ratio.to_s } : { reason: compressed ? "undefined_ratio" : "missing_artifact_bytes" },
      throughput: { value: throughput.to_s }
    }
  }
  [{ ratio: ratio, throughput: throughput }, diagnostic]
end

def score_evidence(path, baseline, ratio_bps, artifacts, verify_bundle: false)
  unless ratio_bps.between?(0, WEIGHT_TOTAL)
    raise InvalidEvidence, "Ratio weight must be between 0 and 10000 basis points"
  end

  if verify_bundle
    raise InvalidEvidence, "Bundle replay uses the saved invocation artifacts" unless artifacts.empty?

    bundle = ReplayBundle.new(path)
    observations, validations = read_records(path, lines: bundle.observation_lines)
  else
    observations, validations = read_records(path)
  end
  verdicts = verify_records(observations, validations)
  runs = verdicts.map { |verdict| verdict.fetch(:run_id) }.uniq
  raise InvalidEvidence, "Scoring requires one run" unless runs.one?

  qualified = verdicts.to_h { |verdict| [verdict.fetch(:observation_id), verdict.fetch(:qualified)] }
  measurements = observations.values.select { |row| row["phase"] == "measurement" }
  if measurements.empty? || measurements.any? { |row| row["operation"] != "encode" }
    raise InvalidEvidence, "Scoring requires encode measurement observations"
  end

  groups = measurements.group_by { |row| row.fetch("config_id") }
  groups.each_value do |rows|
    raise InvalidEvidence, "Multiple observations for one config and slot" if rows.map { |row| slot(row) }.uniq.length != rows.length
  end
  raise InvalidEvidence, "Baseline must name a measured configuration" unless groups.key?(baseline)

  supplied = if bundle
               by_id = observations.values.to_h { |row| [row["observation_id"], row] }
               bundle.verify_invocations(by_id) { |original, artifact| decoded_counts(original, artifact) }
             else
               collect_artifacts(artifacts, measurements.to_h { |row| [row["observation_id"], row] }, qualified)
             end
  configurations = groups.keys.sort
  weights = { ratio: ratio_bps, throughput: WEIGHT_TOTAL - ratio_bps }
  enabled = weights.keys.select { |metric| weights.fetch(metric).positive? }
  scores = configurations.to_h do |config|
    [config, { observations: groups.fetch(config).map { |row| row.fetch("observation_id") }.sort }]
  end
  result = { run_id: runs.first, baseline: baseline, versions: VERSIONS, weights_bps: weights,
             candidate_set: configurations, scores: scores, publication: "withheld_reviewed_uncertainty_required" }

  unless measurements.all? { |row| qualified.fetch(row.fetch("observation_id")) }
    return result.merge(status: "unavailable", reason: "unqualified_evidence")
  end

  workloads = groups.transform_values do |rows|
    rows.to_h { |row| [slot(row), row.values_at("work_bytes", "invocation_count")] }
  end
  unless workloads.values.all? { |workload| workload == workloads.fetch(baseline) }
    return result.merge(status: "unavailable", reason: "incomparable_work")
  end

  digests = {}
  values = configurations.to_h do |config|
    metrics, diagnostic = cohort_measurements(groups.fetch(config), supplied, digests)
    scores.fetch(config).merge!(diagnostic)
    [config, metrics]
  end

  values.each do |config, metrics|
    terms = enabled.map do |metric|
      { direction: "max", weight: Rational(weights.fetch(metric), WEIGHT_TOTAL),
        candidate: metrics[metric], reference: values.fetch(baseline)[metric] }
    end
    value = weighted_score(terms)
    reason = value
    if reason == "unavailable"
      reason = terms.any? { |term| term[:candidate].nil? || term[:reference].nil? } ?
        "missing_enabled_metric" : "nonpositive_enabled_metric"
    end
    scores.fetch(config)[:composite] = value.is_a?(String) ? { reason: reason } : { value: value }
  end

  if values.values.any? { |metrics| enabled.any? { |metric| metrics[metric].nil? } }
    return result.merge(status: "unavailable", reason: "missing_enabled_metric")
  end

  keys = values.transform_values { |metrics| enabled.map { |metric| metrics.fetch(metric) } }
  keys.each do |config, key|
    scores.fetch(config)[:rank] = 1 + keys.values.count { |other| (other <=> key).positive? }
  end
  result.merge(status: "complete")
end

if $PROGRAM_NAME == __FILE__
  begin
    path = ARGV.shift
    baseline = nil
    weight = 5000
    artifacts = []
    verify_bundle = false
    until ARGV.empty?
      case ARGV.shift
      when "--baseline" then baseline = ARGV.shift
      when "--ratio-weight-bps" then weight = Integer(ARGV.shift, 10)
      when "--verify-bundle" then verify_bundle = true
      when "--artifact"
        pair = ARGV.shift(3)
        raise InvalidEvidence, "Expected observation ID, original and artifact path" unless pair.length == 3

        artifacts << pair
      else raise InvalidEvidence, "Unexpected scoring option"
      end
    end
    raise InvalidEvidence, "Expected evidence path and --baseline" unless path && baseline

    puts JSON.generate(score_evidence(path, baseline, weight, artifacts, verify_bundle: verify_bundle))
  rescue InvalidEvidence, JSON::ParserError, EncodingError, SystemCallError, ArgumentError, TypeError => error
    warn "Invalid scoring input: #{error.message}"
    exit 1
  end
end
