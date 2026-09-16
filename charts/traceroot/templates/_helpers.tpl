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
{{- /* Numeric, not string: %v renders a large float64 in exponent form, so a string
       compare here rejected perfectly good whole numbers like 2147483647. */ -}}
{{- if ne (float64 $ttl) (float64 (int64 $ttl)) -}}
{{- fail (printf "migrations.retainFinishedSeconds must be a whole number of seconds, got %v" $ttl) -}}
{{- end -}}
{{- if lt (int64 $ttl) (int64 0) -}}
{{- fail (printf "migrations.retainFinishedSeconds must not be negative, got %v" $ttl) -}}
{{- end -}}
{{- if gt (int64 $ttl) (int64 2147483647) -}}
{{- fail (printf "migrations.retainFinishedSeconds must fit in int32 (max 2147483647, about 68 years), got %v -- Kubernetes types ttlSecondsAfterFinished as int32 and would reject the Job" $ttl) -}}
{{- end -}}
ttlSecondsAfterFinished: {{ int64 $ttl }}
{{- end -}}
{{- end }}

{{/*
A ClickHouse identifier that is safe to splice into DDL and into a shell command
line. Both happen in the SQL-gateway hooks, so anything outside this character set
is rejected at render time rather than becoming a syntax error at CREATE USER, or
extra client flags that silently change which account the verification runs as.
*/}}
{{- define "traceroot.sqlGateway.identifier" -}}
{{- $name := .name -}}
{{- $value := .value -}}
{{- if not (regexMatch "^[A-Za-z_][A-Za-z0-9_]*$" $value) -}}
{{- fail (printf "%s must match ^[A-Za-z_][A-Za-z0-9_]*$ (it is used unquoted in ClickHouse DDL and in a shell command), got %q" $name $value) -}}
{{- end -}}
{{- $value -}}
{{- end }}

{{/*
The two gateway accounts and the ClickHouse admin must be three different accounts.
Collapsing any pair removes the isolation the gateway exists to provide: one identity
would hold the writer's SELECT on the physical tables together with the password the
application hands to customer SQL. Checked at render time, because the hook would
otherwise carry it out and report success.
*/}}
{{- define "traceroot.sqlGateway.checkIdentities" -}}
{{- $w := .Values.sqlGateway.writerUser -}}
{{- $r := .Values.sqlGateway.readonlyUser -}}
{{- $a := .Values.clickhouse.auth.username -}}
{{- if eq $w $r -}}
{{- fail (printf "sqlGateway.writerUser and sqlGateway.readonlyUser must be different accounts, both are %q. One account cannot both own the curated views and be the account customer SQL runs as." $w) -}}
{{- end -}}
{{- if eq $w $a -}}
{{- fail (printf "sqlGateway.writerUser must not be the ClickHouse admin (clickhouse.auth.username), both are %q. The views would run as an account with full access." $w) -}}
{{- end -}}
{{- if eq $r $a -}}
{{- fail (printf "sqlGateway.readonlyUser must not be the ClickHouse admin (clickhouse.auth.username), both are %q. Customer SQL would run with full access." $r) -}}
{{- end -}}
{{- end }}

{{/*
A value that is spliced into a shell command line and into SQL string literals, so it
is held to a conservative character set rather than quoted and hoped for. Wider than
the identifier check above, since this one applies to an existing account name the
chart did not choose and must not break: dots, hyphens and @ are all legal here.
*/}}
{{- define "traceroot.sqlGateway.shellSafe" -}}
{{- $name := .name -}}
{{- $value := .value -}}
{{- if not (regexMatch "^[A-Za-z0-9_.@-]+$" $value) -}}
{{- fail (printf "%s must match ^[A-Za-z0-9_.@-]+$ (it is used in a shell command and in SQL string literals), got %q" $name $value) -}}
{{- end -}}
{{- $value -}}
{{- end }}

{{/*
A settings-profile cap. ClickHouse reads 0 as "no limit", so a cap set to zero is not
a small budget but the absence of one, and the hook would provision it and report
success. Negatives are rejected for the same reason: the profile would hold a value
that bounds nothing.
*/}}
{{- define "traceroot.sqlGateway.limit" -}}
{{- $name := .name -}}
{{- $value := .value -}}
{{- if not (or (kindIs "float64" $value) (kindIs "int64" $value) (kindIs "int" $value)) -}}
{{- fail (printf "sqlGateway.limits.%s must be a positive whole number, got %v (%s)" $name $value (kindOf $value)) -}}
{{- end -}}
{{- if le (int64 $value) (int64 0) -}}
{{- fail (printf "sqlGateway.limits.%s must be greater than zero, got %v. ClickHouse reads 0 as no limit, so this would remove the cap rather than tighten it." $name $value) -}}
{{- end -}}
{{- int64 $value -}}
{{- end }}
