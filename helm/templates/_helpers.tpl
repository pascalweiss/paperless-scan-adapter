{{/*
Expand the name of the chart.
*/}}
{{- define "paperless-scan-adapter.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "paperless-scan-adapter.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "paperless-scan-adapter.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "paperless-scan-adapter.labels" -}}
helm.sh/chart: {{ include "paperless-scan-adapter.chart" . }}
{{ include "paperless-scan-adapter.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "paperless-scan-adapter.selectorLabels" -}}
app.kubernetes.io/name: {{ include "paperless-scan-adapter.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "paperless-scan-adapter.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "paperless-scan-adapter.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return the name of the ConfigMap holding application configuration.
*/}}
{{- define "paperless-scan-adapter.configMapName" -}}
{{- if and .Values.config.nameOverride (ne .Values.config.nameOverride "") }}
{{- .Values.config.nameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-config" (include "paperless-scan-adapter.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Return the name of the PersistentVolumeClaim used for the data volume.
*/}}
{{- define "paperless-scan-adapter.pvcName" -}}
{{- if .Values.persistence.existingClaim }}
{{- .Values.persistence.existingClaim | trunc 63 | trimSuffix "-" }}
{{- else if .Values.persistence.claimName }}
{{- .Values.persistence.claimName | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-data" (include "paperless-scan-adapter.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Return the name of the PersistentVolume if it is managed by this chart.
*/}}
{{- define "paperless-scan-adapter.pvName" -}}
{{- if and .Values.persistence.persistentVolume .Values.persistence.persistentVolume.name }}
{{- .Values.persistence.persistentVolume.name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-pv" (include "paperless-scan-adapter.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
