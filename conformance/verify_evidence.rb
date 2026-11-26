# Independent reader for the experimental IBS-0003 conformance checks.
gem "json", ">= 2.18.0"
gem "json_schemer", "2.4.0"
require "json"
require "json_schemer"

class InvalidEvidence < StandardError; end

class ExactCounter
  MAX = (1 << 53) - 1

  # JSON passes the original decimal/exponent spelling before conversion to Float.
  def self.try_convert(token)
    sign, whole, fraction, exponent = token.match(/\A(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?\z/).captures
    digits = (whole + fraction.to_s).sub(/\A0+/, "")
    return 0 if digits.empty?

    shift = exponent.to_i - fraction.to_s.length
    if shift < 0
      places = -shift
      if places >= digits.length || !digits.end_with?("0" * places)
        raise InvalidEvidence, "Fractional counter"
      end
      digits = digits[0...-places]
    else
      raise InvalidEvidence, "Counter out of range" if digits.length + shift > 16

      digits += "0" * shift
    end
    value = digits.to_i
    raise InvalidEvidence, "Counter out of range" if value > MAX

    sign.empty? ? value : -value
  end
end

def read_records(path)
  schema_dir = File.expand_path("../schemas", __dir__)
  schemas = %w[observation validation].to_h do |kind|
    schema_path = File.join(schema_dir, "#{kind}.schema.json")
    [kind, JSONSchemer.schema(JSON.parse(File.read(schema_path)))]
  end
  observations = {}
  validations = {}
  File.foreach(path, encoding: "UTF-8") do |line|
    raise InvalidEvidence, "Invalid UTF-8" unless line.valid_encoding?

    # The Ruby parser accepts comments. Slashes outside JSON strings are invalid.
    if line.gsub(/"(?:[^"\\]++|\\.)*+"/, "").include?("/")
      raise InvalidEvidence, "Unexpected slash outside JSON string"
    end
    record = JSON.parse(line, allow_duplicate_key: false, decimal_class: ExactCounter)
    kind = record.is_a?(Hash) && record.key?("validation_id") ? "validation" : "observation"
    raise InvalidEvidence, "Invalid #{kind} schema" unless schemas.fetch(kind).valid?(record)

    unless record.values.grep(String).all?(&:valid_encoding?)
      raise InvalidEvidence, "Invalid Unicode string"
    end

    key = record.values_at("run_id", "#{kind}_id")
    index = kind == "validation" ? validations : observations
    raise InvalidEvidence, "Duplicate #{kind} identity #{key.inspect}" if index.key?(key)

    index[key] = record
  end
  [observations, validations]
end

def verify(path)
  verify_records(*read_records(path))
end

def verify_records(observations, validations)
  raise InvalidEvidence, "No observations" if observations.empty?

  checks = {}
  validations.each do |(run_id, validation_id), validation|
    key = [run_id, validation.fetch("observation_id")]
    observation = observations[key]
    unless observation && observation["validation_ref"] == validation_id
      raise InvalidEvidence, "Unreferenced validation #{validation_id.inspect}"
    end

    expected = validation.fetch("cadence") == "last_invocation" ? 1 : observation.fetch("invocation_count")
    unless validation.fetch("checked_invocations") == expected
      raise InvalidEvidence, "Validation cadence and count disagree for #{key.inspect}"
    end

    checks[key] = validation
  end

  observations.map do |key, observation|
    validation = checks[key]
    if observation.key?("validation_ref") && validation.nil?
      raise InvalidEvidence, "Missing validation for #{key.inspect}"
    end

    qualified = observation.values_at("phase", "status") == ["measurement", "ok"] &&
                !validation.nil? &&
                validation.values_at("status", "checked_invocations") == ["verified", observation["invocation_count"]]
    { run_id: key[0], observation_id: key[1], qualified: qualified }
  end
end

if $PROGRAM_NAME == __FILE__
  if ARGV.length != 1
    warn "Usage: ruby conformance/verify_evidence.rb PATH"
    exit 2
  end

  begin
    verify(ARGV.fetch(0)).each { |verdict| puts JSON.generate(verdict) }
  rescue InvalidEvidence, JSON::ParserError, EncodingError, SystemCallError => error
    warn "Invalid evidence: #{error.message}"
    exit 1
  end
end
