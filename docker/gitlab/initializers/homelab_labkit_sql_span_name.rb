# frozen_string_literal: true

# Mounted read-only into gitlab-rails/config/initializers (docker-compose.yml).
#
# With GITLAB_TRACING set, LabKit subscribes SqlInstrumenter to
# sql.active_record. Its span_name does `"active_record:" + payload[:name]`,
# but some GitLab queries pass a Symbol as the query name (e.g.
# :select_seq_start in the partition manager). The TypeError is raised in the
# subscriber's start, and Rails 7.2 re-raises subscriber errors, so the query
# never runs (issue #163: partition maintenance failed on every run).
#
# Hand span_name a copy of the payload with the name as a String. The payload
# that other subscribers see is not changed. Guarded so a GitLab/LabKit
# upgrade that renames or fixes the class turns this into a no-op. Remove once
# upstream converts the name itself.
if defined?(Labkit::Tracing::Rails::ActiveRecord::SqlInstrumenter) &&
    Labkit::Tracing::Rails::ActiveRecord::SqlInstrumenter.method_defined?(:span_name)
  module HomelabLabkitSqlSpanName
    def span_name(payload)
      name = payload[:name]
      return super if name.nil? || name.is_a?(String)

      super(payload.merge(name: name.to_s))
    end
  end

  Labkit::Tracing::Rails::ActiveRecord::SqlInstrumenter.prepend(HomelabLabkitSqlSpanName)
end
