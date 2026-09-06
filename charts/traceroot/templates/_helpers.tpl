{{- define "traceroot.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "traceroot.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "traceroot.selectorLabels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "traceroot.clickhouse.hostname" -}}
{{- if .Values.clickhouse.deploy -}}
{{- printf "%s-clickhouse" (include "traceroot.fullname" .) -}}
{{- else -}}
{{- .Values.clickhouse.host -}}
{{- end -}}
{{- end }}

{{/*
Emits ttlSecondsAfterFinished for a migration Job, or nothing when retention is
null. Validated rather than interpolated: the value reaches here as float64 from
values.yaml but int64 from --set, and anything else -- a list, a map, a string, a
bool, a fractional number -- would otherwise render a field the API server rejects
in the middle of an upgrade, with an error that says nothing about which value.
*/}}
{{- define "traceroot.migrations.ttl" -}}
{{- $ttl := .Values.migrations.retainFinishedSeconds -}}
{{- if not (kindIs "invalid" $ttl) -}}
{{- if not (or (kindIs "float64" $ttl) (kindIs "int64" $ttl) (kindIs "int" $ttl)) -}}
{{- fail (printf "migrations.retainFinishedSeconds must be a whole number of seconds or null, got %v (%s)" $ttl (kindOf $ttl)) -}}
{{- end -}}
{{- if ne (printf "%v" $ttl) (printf "%v" (floor $ttl)) -}}
{{- fail (printf "migrations.retainFinishedSeconds must be a whole number of seconds, got %v" $ttl) -}}
{{- end -}}
{{- if lt (int64 $ttl) (int64 0) -}}
{{- fail (printf "migrations.retainFinishedSeconds must not be negative, got %v" $ttl) -}}
{{- end -}}
ttlSecondsAfterFinished: {{ int64 $ttl }}
{{- end -}}
{{- end }}
