$(function () {
    bindBuyButton();
});

$("img.lazy").lazyload({
    threshold: 300
});


function bindBuyButton(){
    $(".addtocart").click(function () {
        var quantityAvailable = $(this).parent().find('input.quantityAvailable').first().val();
        var priceId = $(this).parent().find('input.priceId').first().val();
        var quantityToBuy = $(this).parent().find('input.quantityToBuy').first().val();

        if (isNumber(quantityToBuy)) {
            if (parseInt(quantityToBuy) > parseInt(quantityAvailable))
                $('<div></div>')
                  .html("Only " + quantityAvailable + " is available.")
                  .dialog({
                      autoOpen: true,
                      modal: true,
                      title: 'Invalid Quantity Selected',
                      closeText: '',
                      buttons: [
                        {
                            text: "OK",
                            "class": "smallGreyButton",
                            click: function () {
                                $(this).dialog("close");
                            }
                        },
                      ]
                  });
            else
                window.location = "/shoppingcart.aspx?id=" + priceId + "&q=" + quantityToBuy;
        }
        else {
            $('<div></div>')
                .html("Only Numbers are allowed.")
                .dialog({
                    autoOpen: true,
                    modal: true,
                    title: 'Invalid Quantity Selected',
                    closeText: '',
                    buttons: [
                        {
                            text: "OK",
                            "class": "smallGreyButton",
                            click: function () {
                                $(this).dialog("close");
                            }
                        },
                    ]
                });
            return;
        }
    });
}

function isNumber(tval)
{
    var re = /^(\s|\d)+$/;
    return re.test(tval);
}

function ChangePriceSort(selectedValue) {
    document.location.href = location.href + '?sortOption=' + selectedValue;
}

function closeTip(tipId, htmlId) {
    $('#' + htmlId).hide();

    $.ajax({
        url: SITEROOT + 'product/closetip?tipId=' + tipId,
        type: 'GET'
    });
}

function AddCart(storePriceId, isSubmitValidation) {
    var formId = '#AddToCart_' + storePriceId;
    var quantityRequested = $(formId + ' #quantityToBuy').val();
    var quantityAvailable = $(formId + ' #quantityAvailable').val();

    var re = /^\s*\d*[1-9]+\d*\s*$/;
    if (re.test(quantityRequested) == false) {
        $('<div></div>')
           .html("Please enter a quantity greater than 0.")
           .dialog({
               autoOpen: true,
               modal: true,
               title: 'Invalid Quantity Selected',
               closeText: '',
               buttons: [
                    {
                        text: "OK",
                        "class": "smallGreyButton",
                        click: function () {
                            $(this).dialog("close");
                        }
                    },
               ]
           });
        return false;
    }

    if (parseInt(quantityRequested) > parseInt(quantityAvailable)) {
        $('<div></div>')
            .html("Please enter a quantity less than or equal to the quantity available.")
            .dialog({
                autoOpen: true,
                modal: true,
                title: 'Invalid Quantity Selected',
                closeText: '',
                buttons: [
                    {
                        text: "OK",
                        "class": "smallGreyButton",
                        click: function () {
                            $(this).dialog("close");
                        }
                    },
                ]
            });
        return false;
    }

    if (isSubmitValidation) {
        return true;
    } else {
        var addToCartButton = document.getElementById('btnAddToCart_' + storePriceId);
        if (addToCartButton) {
            addToCartButton.disabled = true;
        }

        if (analyticsHelper && analyticsEventData) {
            analyticsHelper.trackListoProductAddToCart(analyticsEventData);
        }
        $(formId).submit();
    }
}

function FilterStore(storeId, activeStore) {
    $.ajax({ url: SITEROOT + "FilterStore.ashx", cache: false, data: { store: storeId, active: activeStore }, success: function (data, textStatus, xhrAjax) { if (data == "1") location.reload(); } });
}

function makeImageHover() {
    $('#cardImage').on("load",function () {
        $('#cardImage').show();
    });

    $('#cardImage').on("error", function () {
        var image = $(this).attr('src');
        var lastPart = image.split('/');
        var imageSplit = image.split(lastPart[lastPart.length - 1]);
        var placeHolder = imageSplit[0] + '0.jpg';
        $(this).attr('src', placeHolder);
    });

    $('.imageHover').click(function (event) {
        event.preventDefault();

        $('#cardImage').hide();
        $('#cardImage').attr('src', $(event.currentTarget).attr('image') + "?" + new Date().getTime());

        // Calc x,y
        var x = event.pageX + 15;
        var y = event.pageY;

        if ((y + $('#cardImage').outerHeight()) >= ($(window).height() + $(window).scrollTop()))
            y -= 350;

        $('#imageHover').css('left', x);
        $('#imageHover').css('top', y);
        $('#imageHover').css('display', 'block');
        $("#imageHover").css("position", "absolute");
    });
}

function hideImageHover() {
    $('#imageHover').css('display', 'none');
}

function initializeBackToTopIcon()
{
    // hide #back-top first
    $("#back-top").hide();

    $(window).scroll(function () {
        if ($(this).scrollTop() > 100) {
            $('#back-top').fadeIn();
        } else {
            $('#back-top').fadeOut();
        }
    });

    // scroll body to 0px on click
    $('#back-top a').click(function () {
        $('body,html').animate({
            scrollTop: 0
        }, 800);
        return false;
    });
}

function getParameterByName(name) {
    var match = RegExp('[?&]' + name + '=([^&]*)').exec(window.location.search);
    return match && decodeURIComponent(match[1].replace(/\+/g, ' '));
}

function generateListoImageUrl(baseUrl, imageFileName, options) {
    if (!baseUrl || !imageFileName) {
        return;
    }

    var edits = options || {};
    var width = edits.width || 0;
    var height = edits.height || 0;
    var quality = edits.quality || 0;

    var imageRequest = {};
    imageRequest.key = imageFileName;
    imageRequest.edits = {};
    if (height > 0 || width > 0) {
        imageRequest.edits.resize = {};
        if (height > 0) {
            imageRequest.edits.resize.height = height;
        }
        if (width > 0) {
            imageRequest.edits.resize.width = width;
        }
    }
    if (quality > 0) {
        imageRequest.edits.jpeg = { quality: quality };
    }

    const renderedUrl = baseUrl + btoa(JSON.stringify(imageRequest));
    return renderedUrl;
}

var loadKickbacks = true;
function loadKickbackSummary(siteRoot) {
    if (loadKickbacks) {
        $.ajax({
            url: siteRoot + '/shoppingcart/kickbacksummary',
            type: "GET",
            dataType: 'html',
            xhrFields: {
                withCredentials: true
            },
            cache: false,
            success: function (data) {
                $('.kickbacksPlaceholder').html(data);
            }
        });

        loadKickbacks = false;
    }
}
