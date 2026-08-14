variable "name_prefix" {
  type    = string
  default = "fraud-lake"
}

variable "region" {
  type = string
}

variable "account_id" {
  type = string
}

variable "source_root" {
  type = string
}

variable "stream_arn" {
  description = "Kinesis stream to consume. The module is only instantiated when the stream exists."
  type        = string
}
