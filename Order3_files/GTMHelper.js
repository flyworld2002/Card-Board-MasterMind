var url = window.location.href.split('?')[0];

window.onload = function () { trackLinks(document); };

function trackEvent(category, action, label) {
    var data = {
        "event": "trackEvent",
        "event_category": category,
        "event_action": action,
        "event_label": label,
        "logged_in": gtmData.user !== "",
        "user_id": gtmData.user,
        "url": url
    };

    if (gtmData.sellerTrackingKey) {
        data.tracking_id = gtmData.sellerTrackingKey;
    }

    dataLayer.push(data);
}

function generateProductModel(name, id, line, position, list, price, condition, set) {
    var data = {
        "product_id": id,
        "product_name": name,
        "product_line": line,
        "list": list,
        "position": position, 
        "price": price || '',
        "condition": condition || '',
        "set_name": set || ''
    };
    return data;
}

function generateEcommerceEvent(event, label) {
    var data = {
        "event": event,
        "ecommerce": {
            "currencyCode": "USD"
        },
        "logged_in": gtmData.user !== "",
        "user_id": gtmData.user,
        "url": url
    };

    if (gtmData.pro_store) {
        data.pro_store = gtmData.pro_store;
    }

    if (gtmData.sellerTrackingKey) {
        data.tracking_id = gtmData.sellerTrackingKey;
    }

    return data;
}

function generateProductEvent(event, label, products) {
    var data = generateEcommerceEvent(event, label);
    data.ecommerce[label] = {
        "products": products
    };
    return data;
}

function trackCheckoutEvent(event, label, actionField, products) {
    var data = generateEcommerceEvent(event, label);
    data.ecommerce[label] = {
        "actionField": actionField,
        "products": products
    };

    if (gtmData.sellerTrackingKey) {
        data.tracking_id = gtmData.sellerTrackingKey;
    }

    dataLayer.push(data);
}

function trackEnhancedEcommerce(value, transactionId, email) {
    data = {
        'send_to': 'AW-835410317',
        value: value,
        currency: 'USD',
        'transaction_id': transactionId,
        email: email,
    }

    dataLayer.push([ 'event', 'conversion', data ])
}

function trackProductsEvent(event, label, products) {
    dataLayer.push(generateProductEvent(event, label, products));
}

function trackProductEvent(event, label, name, id, line, position, list) {
    var data = [generateProductModel(name, id, line, position, list)];
    trackProductsEvent(event, label, data);
}

function AddToCartAndTrack(productIdentifier) {
    var formId = '#AddToCart_' + productIdentifier;
    var quantityRequested = $(formId + ' #quantityToBuy').val();

    var product = gtmData.productData[productIdentifier];
    product.quantity = quantityRequested;
    trackProductsEvent('addToCart', 'add', [product]);

    return AddCart(productIdentifier, true);
}

function trackLinks(d) {
    Array.from(d.links).forEach(function (e) {
        var trackf = null;
        if (e.host.toLowerCase().includes("tcgplayer")) {
            if (location.host.toLowerCase() !== e.host.toLowerCase()) {
                trackf = function () {
                    trackEvent('Cross Domain Link', e.host, e.href);
                };
            }
        } else {
            trackf = function () {
                trackEvent('Exit Link', e.text.trim(), e.href);
            };
        }
        if (trackf != null) {
            if (e.onclick != null) {
                var original = e.onclick;
                e.onclick = function () {
                    original();
                    trackf();
                };
            } else {
                e.onclick = trackf;
            }
        }
    });
}

// Google Analytics 4

function GA4generateEcommerceEvent(event, ecommerce) {
    // Clear previous object
    dataLayer.push({ ecommerce: null })
    dataLayer.push({
        event: event,
        ecommerce: ecommerce
    })
}

// https://developers.google.com/analytics/devguides/collection/ga4/reference/events?client_type=gtag#purchase
function GA4purchaseEvent(transactionId, value, currency, shipping, items, tax) {
    GA4generateEcommerceEvent('purchase', {
        transaction_id: transactionId,
        value: value,
        shipping: shipping,
        currency: currency,
        tax: tax,
        items: items
    })
}
