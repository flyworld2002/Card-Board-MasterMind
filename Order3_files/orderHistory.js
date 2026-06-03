function showTransactionModal(title, rateSellerText, transactionNumber, isDirect) {
    $('#RateTransactionButton_' + transactionNumber).addClass('disabled');
    $('#selectedRating').val(0);
    $('.negativeFeedbackWarning').hide();
    $('.neutralFeedbackWarning').hide();
    $('.positiveFeedbackWarning').hide();
    $('#ratingDesc').val('');
    $('#transactionNumber').val(transactionNumber);
    $('#isDirect').val(isDirect);
    $('#rateSellerValidation').css('visibility', 'hidden');
    $('#rateSellerValidation').css('display', 'none');
    $('#ratingDescValidation').css('visibility', 'hidden');
    $('#ratingDescValidation').css('display', 'none');
    $('#Question2Yes').prop('checked', false);
    $('#Question2No').prop('checked', false);
    $('#Question3Yes').prop('checked', false);
    $('#Question3No').prop('checked', false);
    $('#Question4Yes').prop('checked', false);
    $('#Question4No').prop('checked', false);
    $('#Question4NA').prop('checked', false);
    $('#ratingDesc').limit('400', '#charsLeft');

    $.ajax({
        url: SITEROOT + 'ratetransaction/getfeedbackfromordernumber?r=' + Math.floor(Math.random() * 165468787) + '&orderNumber=' + transactionNumber + '&isDirect=' + isDirect,
        type: 'POST',
        dataType: 'json',
        data: null,
        cache: false,
        contentType: 'application/json',
        async: true
    }).done(function (data) {
        if (data == "{}") {
            $('#deleteFeedback').css('display', 'none');
            $('#submitFeedback').css('display', 'inline-block');
            $('.feedbackInput').removeClass('disabled');
            $('#Question2Yes').removeAttr('disabled');
            $('#Question2No').removeAttr('disabled');
            $('#Question3Yes').removeAttr('disabled');
            $('#Question3No').removeAttr('disabled');
            $('#Question4Yes').removeAttr('disabled');
            $('#Question4No').removeAttr('disabled');
            $('#Question4NA').removeAttr('disabled');
        }
        else {
            // databind search results
            $('#selectedRating').val(data.Question1);

            if (data.Question1 < 3) {
                $('.negativeFeedbackWarning').show();
            } else if (data.Question1 === 3) {
                $('.neutralFeedbackWarning').show();
            } else {
                $('.positiveFeedbackWarning').show();
            }

            $('#ratingDesc').val(data.Question5);
            $('#Question2Yes').prop('checked', data.Question2 == 1);
            $('#Question2No').prop('checked', data.Question2 == 2);
            $('#Question3Yes').prop('checked', data.Question3 == 1);
            $('#Question3No').prop('checked', data.Question3 == 2);
            $('#Question4Yes').prop('checked', data.Question4 == 1);
            $('#Question4No').prop('checked', data.Question4 == 2);
            $('#Question4NA').prop('checked', data.Question4 == 3);
            $('#Question2Yes').attr('disabled', 'disabled');
            $('#Question2No').attr('disabled', 'disabled');
            $('#Question3Yes').attr('disabled', 'disabled');
            $('#Question3No').attr('disabled', 'disabled');
            $('#Question4Yes').attr('disabled', 'disabled');
            $('#Question4No').attr('disabled', 'disabled');
            $('#Question4NA').attr('disabled', 'disabled');
            $('#submitFeedback').css('display', 'none');
            $('#deleteFeedback').css('display', 'inline-block');
            $('.feedbackInput').addClass('disabled');
        }

        resetStarRating();
        var dialog = $('.rateBox');
        $('.rateTitle').text(title);
        $('#rateSellerText').text(rateSellerText);
        var overlay = $(".pageOverlay");
        overlay.height($('body').height() + 8);
        overlay.show();

        var top = 100 + $(window).scrollTop();
        var left = ($(window).width() / 2) - (dialog.width() / 2) + $(window).scrollLeft();
        dialog.css({ top: top, left: left, position: 'absolute' });
        dialog.show();
        dialog.css('visibility', 'visible');
        $('#RateTransactionButton_' + transactionNumber).removeClass('disabled');
    }).fail(function () {
        $('#RateTransactionButton_' + transactionNumber).removeClass('disabled');
        showError("Your feedback could not be retrieved. Please refresh the browser and try again.");
    });
}

function hideTransactionModal(title) {
    //Display the message
    var dialog = $('.rateBox');    
    var overlay = $(".pageOverlay");
    dialog.css('visibility', 'hidden');
    dialog.hide();    
    overlay.hide();
}

function submitRating() {
    var isValid = true;
    var selectedRating = $('#selectedRating').val();
    var ratingDesc = $('#ratingDesc').val();

    if (selectedRating == '0') {
        $('#rateSellerValidation').css('visibility', 'visible');
        $('#rateSellerValidation').css('display', 'block');
        isValid = false;
    }
    else {
        $('#rateSellerValidation').css('visibility', 'hidden');
        $('#rateSellerValidation').css('display', 'none');
    }
    if (selectedRating != 5 && (ratingDesc == undefined || ratingDesc == '')) {
        $('#ratingDescValidation').css('visibility', 'visible');
        $('#ratingDescValidation').css('display', 'block');
        isValid = false;
    }
    else {
        $('#ratingDescValidation').css('visibility', 'hidden');
        $('#ratingDescValidation').css('display', 'none');
    }

    if (isValid) {
        startWait();
        var ratingData = {
            TransactionNumber: $('#transactionNumber').val(),
            IsDirect: $('#isDirect').val(),
            Question1: $('#selectedRating').val(),
            Question2: $('#Question2Yes').is(':checked') ? "1" : $('#Question2No').is(':checked') ? "2" : "0",
            Question3: $('#Question3Yes').is(':checked') ? "1" : $('#Question3No').is(':checked') ? "2" : "0",
            Question4: $('#Question4Yes').is(':checked') ? "1" : $('#Question4No').is(':checked') ? "2" : $('#Question4NA').is(':checked') ? "3" : "0",
            Question5: $('#ratingDesc').val()
        };

        $.ajax({
            url: SITEROOT + 'ratetransaction/savefeedback?r=' + Math.floor(Math.random() * 165468787),
            type: 'POST',
            data: JSON.stringify({
                'sellerOrderFeedback': ratingData
            }),
            dataType: 'json',
            cache: false,
            contentType: 'application/json',
            async: true,
            success: function (data) {
                if (data != null && !data.Success) {
                    stopWait();
                    hideTransactionModal();
                    showError(data.Message)
                }
                else {
                    stopWait();
                    hideTransactionModal();
                    $('#RateTransactionButton_' + $('#transactionNumber').val()).attr('value', "Edit Rating");
                }
            },
            error: function () {
                stopWait();
                hideTransactionModal();
                showError("An error occurred while saving your feedback. Please refresh the browser and try again.");
            }
        });
    }
}

function deleteRating() {
    startWait();
    $.ajax({
        url: SITEROOT + 'ratetransaction/deletefeedback?r=' + Math.floor(Math.random() * 165468787) + '&orderNumber=' + $('#transactionNumber').val() + '&isDirect=' + $('#isDirect').val(),
        type: 'POST',
        dataType: 'json',
        data: null,
        cache: false,
        contentType: 'application/json',
        async: true,
        success: function (data) {
            if (data != null && !data.Success) {
                stopWait();
                hideTransactionModal();
                showError(data.Message)
            }
            else
            {
                stopWait();
                hideTransactionModal();
                $('#RateTransactionButton_' + $('#transactionNumber').val()).attr('value', $('#isDirect').val().toString().toLowerCase() == 'true' ? "Rate Package" : "Rate Transaction");
            }
        },
        error: function () {
            stopWait();
            hideTransactionModal();
            showError('An error occurred while deleting your feedback. Please refresh the browser and try again.');
        }
    });
}

function resetStarRating() {
    var selectedRating = $('#selectedRating').val();

    if (selectedRating > 0 && selectedRating < 3) {
        $('.negativeFeedbackWarning').show();
        $('.neutralFeedbackWarning').hide();
        $('.positiveFeedbackWarning').hide();
    } else if (selectedRating == 3) {
        $('.negativeFeedbackWarning').hide();
        $('.neutralFeedbackWarning').show();
        $('.positiveFeedbackWarning').hide();
    } else if (selectedRating > 0) {
        $('.negativeFeedbackWarning').hide();
        $('.neutralFeedbackWarning').hide();
        $('.positiveFeedbackWarning').show();
    }

    var i = 1;
    while (i <= 5) {
        if (i <= selectedRating) {
            $('#starRating' + i.toString()).removeClass('fa-star-o');
            $('#starRating' + i.toString()).addClass('fa-star');
            $('#starRating' + i.toString()).addClass('starBlue');
        }
        else {            
            $('#starRating' + i.toString()).removeClass('fa-star');
            $('#starRating' + i.toString()).removeClass('starBlue');
            $('#starRating' + i.toString()).addClass('fa-star-o');
        }
        i++;
    }
}

function previewRating(rating) {
    var i = 1;
    while (i <= 5) {
        if (i <= rating) {
            $('#starRating' + i.toString()).removeClass('fa-star-o');
            $('#starRating' + i.toString()).addClass('fa-star');
            $('#starRating' + i.toString()).addClass('starBlue');
        }
        else {
            $('#starRating' + i.toString()).removeClass('fa-star');
            $('#starRating' + i.toString()).removeClass('starBlue');
            $('#starRating' + i.toString()).addClass('fa-star-o');
        }
        i++;
    }
}

function setRating(rating) {
    $('#selectedRating').val(rating);
    resetStarRating();
}

function showError(message) {
    $('#messageModal')
        .html(message)
        .dialog({
            autoOpen: true,
            modal: true,
            title: 'Error',
            closeText: '',
            buttons: [{
                        text: "Close",
                        "class": "smallGreyButton",
                        click: function () {
                            $(this).dialog("close");
                        }
                    }]
        });
}

function startWait() {
    $('.rateBody').addClass('disabled');
}

function stopWait() {
    $('.rateBody').removeClass('disabled');
}
