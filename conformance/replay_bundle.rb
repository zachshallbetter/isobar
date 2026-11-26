# Independently verify the codec benchmark's saved plan and invocation files.
require "digest"

class ReplayBundle
  MAX_METADATA = 8 * 1024 * 1024
  MAX_INPUT = 1024 * 1024
  MAX_ARTIFACT = 2 * MAX_INPUT

  def initialize(path)
    raise InvalidEvidence, "Bundle replay requires observations.jsonl" unless File.basename(path) == "observations.jsonl"

    @root = File.realpath(File.dirname(path))
    @checksums = {}
    read_file("checksums.sha256").each_line do |line|
      entry = /\A([0-9a-f]{64})  (?:\.\/)?([A-Za-z0-9._\/-]+)(?:\r?\n)?\z/.match(line)
      raise InvalidEvidence, "Invalid bundle checksum entry" unless entry

      digest, name = entry.captures
      if name.split("/", -1).any? { |part| ["", ".", ".."].include?(part) } || @checksums.key?(name)
        raise InvalidEvidence, "Invalid or duplicate bundle checksum path"
      end
      @checksums[name] = digest
    end
  end

  def observation_lines
    checked("observations.jsonl").force_encoding("UTF-8").each_line
  end

  def verify_invocations(observations)
    manifest = strict_json(checked("manifest.json").force_encoding("UTF-8"))
    verify_plan(manifest, observations)
    original = checked("input.bin", MAX_INPUT)
    unless counter?(manifest["fixture_bytes"], 0, MAX_INPUT) &&
           original.bytesize == manifest["fixture_bytes"] &&
           Digest::SHA256.hexdigest(original) == manifest["fixture_sha256"]
      raise InvalidEvidence, "Bundle input disagrees with manifest"
    end

    batches = invocation_batches(observations)
    supplied = {}
    observations.each do |id, row|
      count = manifest["#{row['phase']}_invocations_per_batch"]
      batch = batches.fetch(id, {})
      unless counter?(count) && row["invocation_count"] == count &&
             batch.size == count && batch.keys.minmax == [1, count]
        raise InvalidEvidence, "Planned invocation coverage mismatch: #{id}"
      end

      elapsed = 0
      counts = batch.values.map do |call|
        work, ns, size = call.values_at("work_bytes", "elapsed_ns", "artifact_bytes")
        unless counter?(work, 0) && work == original.bytesize && counter?(ns) && counter?(size, 1, MAX_ARTIFACT)
          raise InvalidEvidence, "Invalid invocation counters: #{id}"
        end
        artifact = checked(call.fetch("artifact"), MAX_ARTIFACT)
        raise InvalidEvidence, "Artifact size disagrees with invocation: #{id}" unless artifact.bytesize == size

        elapsed += ns
        yield original, artifact
      end
      unless row["work_bytes"] == original.bytesize * count && row["elapsed_ns"] == elapsed
        raise InvalidEvidence, "Invocation totals disagree with observation: #{id}"
      end
      supplied[id] = counts if row["phase"] == "measurement"
    end
    supplied
  end

  private

  def counter?(value, minimum = 1, maximum = ExactCounter::MAX)
    value.is_a?(Integer) && value.between?(minimum, maximum)
  end

  def read_file(name, limit = MAX_METADATA)
    path = File.join(@root, name)
    unless File.realpath(path) == path && File.file?(path)
      raise InvalidEvidence, "Bundle file must be a regular file inside the bundle: #{name}"
    end
    data = File.binread(path, limit + 1) || "".b
    raise InvalidEvidence, "Bundle file exceeds size limit: #{name}" if data.bytesize > limit

    data
  end

  def checked(name, limit = MAX_METADATA)
    expected = @checksums[name]
    raise InvalidEvidence, "Missing bundle digest: #{name}" unless expected

    data = read_file(name, limit)
    raise InvalidEvidence, "Bundle digest mismatch: #{name}" unless Digest::SHA256.hexdigest(data) == expected

    data
  end

  def verify_plan(manifest, observations)
    unless manifest.is_a?(Hash) && manifest["plan"].is_a?(Array)
      raise InvalidEvidence, "Invalid bundle plan"
    end

    planned = {}
    slots = {}
    manifest.fetch("plan").each do |step|
      raise InvalidEvidence, "Invalid planned step" unless step.is_a?(Hash)

      worker, block, seq, phase, config = step.values_at("worker", "block", "sequence", "phase", "config")
      unless [worker, block, seq].all? { |n| counter?(n) } &&
             %w[warmup measurement].include?(phase) && config.is_a?(String) && !config.empty?
        raise InvalidEvidence, "Invalid planned step"
      end
      key = "w#{worker}-s#{seq}"
      slot = [worker, block, config]
      raise InvalidEvidence, "Duplicate planned step" if planned.key?(key) || slots.key?(slot)

      planned[key] = ["worker-#{worker}", "block-#{block}", seq, phase, config]
      slots[slot] = true
    end

    if planned.empty? || planned.keys.sort != observations.keys.sort
      raise InvalidEvidence, "Planned coverage mismatch"
    end
    unless observations.values.map { |row| row["input_id"] }.uniq.one?
      raise InvalidEvidence, "Bundle plan requires one input"
    end

    observations.each do |id, row|
      unless row.values_at("worker_id", "block_id", "sequence", "phase", "config_id") == planned[id] &&
             row["run_id"] == manifest["run_id"] && row["operation"] == "encode"
        raise InvalidEvidence, "Planned step mismatch: #{id}"
      end
    end
  end

  def invocation_batches(observations)
    batches = Hash.new { |hash, key| hash[key] = {} }
    paths = {}
    checked("invocations.jsonl").force_encoding("UTF-8").each_line do |line|
      call = strict_json(line)
      raise InvalidEvidence, "Invalid bundle invocation" unless call.is_a?(Hash)

      id, number, path = call.values_at("observation_id", "invocation", "artifact")
      unless observations.key?(id) && counter?(number) && path.is_a?(String) &&
             /\Aartifacts\/[A-Za-z0-9_.-]+\z/.match?(path) && !paths.key?(path) && !batches[id].key?(number)
        raise InvalidEvidence, "Invalid or duplicate bundle invocation"
      end
      batches[id][number] = call
      paths[path] = true
    end
    batches
  end
end
