(function () {
    "use strict";

    var maximum = 9007199254740991;

    function number(value) {
        return typeof value === "number" && Number.isFinite(value) &&
            Math.abs(value) <= maximum;
    }

    function equal(left, right) {
        return typeof left === typeof right && left === right;
    }

    function matches(when, answers) {
        if (!Object.prototype.hasOwnProperty.call(answers, when.question) ||
                answers[when.question] === null || answers[when.question] === "") {
            return false;
        }
        var actual = answers[when.question];
        switch (when.op) {
        case "eq":
            return equal(actual, when.value);
        case "in":
            return when.value.some(function (value) { return equal(actual, value); });
        case "gt":
            return number(actual) && actual > when.value;
        case "lt":
            return number(actual) && actual < when.value;
        default:
            throw new Error("invalid_branching_operator");
        }
    }

    function visibleQuestions(definition, answers) {
        var questions = Array.isArray(definition) ? definition : definition.questions;
        var rules = Array.isArray(definition) ? [] : definition.rules;
        var targeted = new Set();
        var visible = [];
        rules.forEach(function (rule) {
            rule.show.forEach(function (key) { targeted.add(key); });
        });
        questions.forEach(function (question) {
            if (!targeted.has(question.id) || rules.some(function (rule) {
                return rule.show.indexOf(question.id) !== -1 &&
                    visible.indexOf(rule.when.question) !== -1 &&
                    matches(rule.when, answers);
            })) {
                visible.push(question.id);
            }
        });
        return visible;
    }

    function enhance(form) {
        if (form.dataset.branchEnhanced) { return; }
        var config = form.querySelector("[data-branch-definition]");
        if (!config) { return; }
        var definition = JSON.parse(config.value);
        form.dataset.branchEnhanced = "true";
        function update() {
            var answers = {};
            definition.questions.forEach(function (question) {
                var control = form.elements.namedItem(question.id);
                if (!control || control.value === "") { return; }
                var value = control.value;
                if (question.type === "boolean") {
                    value = value === "True" ? true : value === "False" ? false : null;
                } else if (question.type === "number") {
                    value = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value) ?
                        Number(value) : null;
                    if (!number(value)) { value = null; }
                }
                answers[question.id] = value;
            });
            var visible = visibleQuestions(definition, answers);
            form.querySelectorAll("[data-question-id]").forEach(function (row) {
                var hidden = visible.indexOf(row.dataset.questionId) === -1;
                row.hidden = hidden;
                row.querySelectorAll("input, select, textarea").forEach(function (control) {
                    control.disabled = hidden;
                });
            });
        }
        form.addEventListener("input", update);
        form.addEventListener("change", update);
        update();
    }

    function start() {
        document.querySelectorAll("[data-branching-form]").forEach(enhance);
    }
    window.ClinicQuestionnaires = Object.freeze({visibleQuestions: visibleQuestions});
    document.addEventListener("DOMContentLoaded", start);
    document.addEventListener("htmx:load", start);
}());
