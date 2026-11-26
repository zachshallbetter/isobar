# Bounded, frame-by-frame replay via libzstd. Artifact parsing and byte
# comparison run in Ruby, independently of the Python evidence scorer.
require "fiddle"

class ZstandardArtifact
  def initialize(path)
    @library = Fiddle.dlopen(path)
    size = Fiddle::TYPE_SIZE_T
    pointer = Fiddle::TYPE_VOIDP
    @frame_size = bind("ZSTD_findFrameCompressedSize", [pointer, size], size)
    @decompress = bind("ZSTD_decompress", [pointer, size, pointer, size], size)
    @is_error = bind("ZSTD_isError", [size], Fiddle::TYPE_INT)
  end

  def matches?(artifact, original)
    source = Fiddle::Pointer[artifact]
    output = Fiddle::Pointer.malloc(original.bytesize + 1, Fiddle::RUBY_FREE)
    consumed = 0
    produced = 0
    while consumed < artifact.bytesize
      remaining = artifact.bytesize - consumed
      frame = source + consumed
      frame_size = @frame_size.call(frame, remaining)
      return false if @is_error.call(frame_size) != 0 || !frame_size.between?(1, remaining)

      capacity = original.bytesize - produced
      decoded_size = @decompress.call(output, capacity, frame, frame_size)
      return false if @is_error.call(decoded_size) != 0 || decoded_size > capacity
      return false unless output[0, decoded_size] == original.byteslice(produced, decoded_size)

      produced += decoded_size
      consumed += frame_size
    end
    produced == original.bytesize
  ensure
    output&.call_free
  end

  private

  def bind(name, arguments, result)
    Fiddle::Function.new(@library[name], arguments, result)
  end
end

def zstandard_matches(artifact, original)
  path = ENV["ISOBAR_ZSTD_LIB"]
  raise InvalidEvidence, "Zstandard replay requires ISOBAR_ZSTD_LIB" if path.nil? || path.empty?

  @zstandard_decoder ||= ZstandardArtifact.new(path)
  @zstandard_decoder.matches?(artifact, original)
rescue Fiddle::DLError => error
  raise InvalidEvidence, "Could not load libzstd: #{error.message}"
end
