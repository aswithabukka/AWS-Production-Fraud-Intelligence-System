variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "region" {
  type = string
}

variable "state_machine_arn" {
  type = string
}

variable "glue_job_names" {
  description = "Glue job names to chart duration for."
  type        = list(string)
}

variable "glue_log_group" {
  description = "Log group the error-log widget queries."
  type        = string
}

variable "max_iterations" {
  description = "Drawn as an annotation line — sustained contact means the router is looping."
  type        = number
  default     = 5
}

variable "alerts_topic_arn" {
  description = "SNS topic the drift alarm notifies."
  type        = string
}
